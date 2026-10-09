"""BƯỚC 3c — SINH VIÊN VIẾT. Tự động hoá runbook §4 "Runbook: Region Chính Down".

7 bước trên slide, mỗi bước 1 dòng log có ts. Log này CHÍNH LÀ timeline của postmortem.
  1 xac_nhan_outage          — probe cả 2 region, đừng tin 1 lần fail (dùng nhiều lần
                              hoặc gọi health_checker.probe nếu đã viết xong 3a)
  2 thong_bao_incident       — ts của dòng này là mốc "operator biết tin", LUÔN LUÔN
                              SAU t_outage trong chaos-events (không thể trùng — operator
                              không thể biết ngay giây outage xảy ra). Ghi cả 2 ts vào
                              log để postmortem tính được "độ trễ thông báo".
  3 scale_gpu_pool           — gọi HÀM `failover.failover(...)` MỘT LẦN DUY NHẤT. Hàm
                              đó tự làm đủ 5 bước con (verify/restore/scale/wait/cutover)
                              và tự ghi log riêng vào reports/failover-events.jsonl.
  4 verify_state_replica     — KHÔNG gọi lại failover — chỉ ĐỌC kết quả (vector count +
                              weights ở region phụ) từ dict mà bước 3 trả về, để log vào
                              runbook-run.jsonl cho postmortem đọc 1 chỗ duy nhất.
  5 dns_cutover              — cũng chỉ đọc lại: kết quả cutover có ok hay không.
  6 verify_golden_signals    — 10 request thật vào region phụ: p95 latency + error rate
  7 post_incident            — elapsed_s + lệnh đo RTO

BÁN TỰ ĐỘNG, KHÔNG FULL-AUTO (§4: "failover đầu tiên nên là bán tự động — alert +
1-click confirm — tránh flapping gây failover 2 chiều liên tục"). Mặc định phải hỏi
người vận hành confirm; --auto chỉ dùng trong CI/khi chấm điểm.

Chạy:  python dr/runbook.py --primary a --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from dr import failover as fo  # noqa: E402

LOG = pathlib.Path("reports/runbook-run.jsonl")
URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}


def step(n, name, **kw):
    now = time.time()
    record = {
        "ts": now,
        "iso": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)
        ),
        "step": n,
        "name": name,
        **kw,
    }

    LOG.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, ensure_ascii=False)

    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(text + "\n")

    print(text, flush=True)
    return record


def confirm(auto: bool, msg: str) -> bool:
    if auto:
        return True

    try:
        return input(f"{msg} [y/N]: ").strip().lower() == "y"
    except EOFError:
        return False


def _events(path: str) -> list:
    file = pathlib.Path(path)
    if not file.exists():
        return []

    records = []
    for line in file.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            # File có thể đang được process khác append.
            continue

    return records


def run(primary: str, target: str, backend: str, auto: bool) -> dict:
    started = time.monotonic()

    if (
        primary not in URL
        or target not in URL
        or primary == target
        or backend not in {"fs", "minio"}
    ):
        return {"ok": False, "error": "invalid_arguments"}

    try:
        # 1. Xác nhận primary lỗi qua ba probe liên tiếp.
        consecutive_fails = 0
        deadline = time.monotonic() + 60.0

        while time.monotonic() < deadline:
            round_started = time.monotonic()

            try:
                response = httpx.get(
                    f"{URL[primary]}/readyz",
                    timeout=2.0,
                )
                healthy = response.status_code == 200
            except httpx.HTTPError:
                healthy = False

            consecutive_fails = (
                0 if healthy else consecutive_fails + 1
            )

            if consecutive_fails >= 3:
                break

            pause = 5.0 - (time.monotonic() - round_started)
            if pause > 0:
                time.sleep(pause)

        if consecutive_fails < 3:
            step(
                1,
                "xac_nhan_outage",
                ok=False,
                primary=primary,
            )
            return {"ok": False, "error": "outage_not_confirmed"}

        # Target phải còn sống, chưa cần ready trước restore.
        response = httpx.get(
            f"{URL[target]}/healthz",
            timeout=2.0,
        )
        response.raise_for_status()

        kills = [
            event
            for event in _events("chaos/chaos-events.jsonl")
            if event.get("action") == "kill"
            and event.get("region") == primary
        ]

        if not kills:
            raise RuntimeError("Không tìm thấy kill event cho primary")

        outage = kills[-1]
        t_outage = outage["ts"]

        # Chờ checker riêng phát hiện đúng outage hiện tại.
        detection = None
        deadline = time.monotonic() + 60.0

        while time.monotonic() < deadline:
            matches = [
                event
                for event in _events("reports/health-events.jsonl")
                if event.get("event") == "state_change"
                and event.get("region") == primary
                and event.get("to") == "UNHEALTHY"
                and event.get("ts", 0) >= t_outage
            ]

            if matches:
                detection = matches[0]
                break

            time.sleep(0.5)

        if detection is None:
            raise RuntimeError(
                "Chưa có health-check detection hợp lệ"
            )

        step(
            1,
            "xac_nhan_outage",
            ok=True,
            primary=primary,
            target=target,
            consecutive_fails=consecutive_fails,
            t_detect=detection["ts"],
        )

        # 2. Mở incident, phân biệt outage và thông báo.
        operator_ts = time.time()

        step(
            2,
            "thong_bao_incident",
            t_outage=t_outage,
            outage_iso=outage.get("iso"),
            operator_ts=operator_ts,
            notification_delay_s=round(
                operator_ts - t_outage, 3
            ),
            mode="auto" if auto else "manual",
        )

        if not confirm(
            auto,
            f"Failover từ {primary} sang {target}?",
        ):
            return {"ok": False, "error": "operator_cancelled"}

        operator_confirm_ts = time.time()

        # 3. Gọi failover đúng một lần.
        result = fo.failover(target, backend, wait=60.0)

        step(
            3,
            "scale_gpu_pool",
            ok=result.get("ok", False),
            operator_confirm_ts=operator_confirm_ts,
            result=result,
        )

        if not result.get("ok"):
            step(
                7,
                "post_incident",
                ok=False,
                elapsed_s=round(
                    time.monotonic() - started, 3
                ),
                error=result.get("error"),
            )
            return result

        # 4. Chỉ đọc kết quả replica từ failover.
        state = result.get("state", {})
        replica_ok = (
            bool(state.get("weights"))
            and state.get("count", 0) > 0
            and state.get("pool_state") == "full"
        )

        step(
            4,
            "verify_state_replica",
            ok=replica_ok,
            count=state.get("count"),
            weights=state.get("weights"),
            embed_model_version=result.get(
                "embed_model_version"
            ),
        )

        if not replica_ok:
            raise RuntimeError("Replica verification failed")

        # 5. Xác nhận pointer, không cutover thêm lần nữa.
        active = pathlib.Path(
            "edge/active_region"
        ).read_text().strip()

        cutover_ok = active == target

        step(
            5,
            "dns_cutover",
            ok=cutover_ok,
            active_region=active,
            cutover_ts=result.get("cutover_ts"),
        )

        if not cutover_ok:
            raise RuntimeError("Cutover verification failed")

        # 6. Kiểm tra bằng 10 request thật tới target.
        latencies = []
        errors = 0

        with httpx.Client(timeout=3.0) as client:
            for index in range(10):
                request_started = time.monotonic()

                try:
                    response = client.get(
                        f"{URL[target]}/v1/infer",
                        params={"q": f"golden signal {index}"},
                    )
                    body = response.json()
                    ok = (
                        response.status_code == 200
                        and body.get("region") == target
                    )
                except (httpx.HTTPError, ValueError):
                    ok = False

                latencies.append(
                    (time.monotonic() - request_started) * 1000
                )
                errors += int(not ok)

        # Nearest-rank p95 của 10 mẫu là mẫu lớn nhất.
        p95_ms = round(sorted(latencies)[-1], 2)
        error_rate = errors / 10

        step(
            6,
            "verify_golden_signals",
            ok=errors == 0,
            requests=10,
            errors=errors,
            p95_ms=p95_ms,
            error_rate=error_rate,
        )

        # 7. Tổng kết; RTO thật vẫn đo bằng loadgen.
        step(
            7,
            "post_incident",
            ok=errors == 0,
            elapsed_s=round(
                time.monotonic() - started, 3
            ),
            measure_command=(
                "python3 tools/measure_rto.py "
                "--loadgen reports/drill-2-withdr.jsonl "
                "--target-rto 300"
            ),
        )

        return {
            "ok": errors == 0,
            "target": target,
            "p95_ms": p95_ms,
            "error_rate": error_rate,
            "failover": result,
        }

    except (Exception, SystemExit) as exc:
        error = f"{type(exc).__name__}: {exc}"

        step(
            7,
            "post_incident",
            ok=False,
            elapsed_s=round(
                time.monotonic() - started, 3
            ),
            error=error,
        )

        return {"ok": False, "error": error}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--primary", default="a")
    p.add_argument("--target", default="b")
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--auto", action="store_true")
    a = p.parse_args()
    print(json.dumps(run(a.primary, a.target, a.backend, a.auto), indent=2))

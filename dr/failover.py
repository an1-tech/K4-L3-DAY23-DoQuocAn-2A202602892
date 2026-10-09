"""BƯỚC 3b — SINH VIÊN VIẾT. Cutover sang region phụ.

5 bước, THỨ TỰ QUAN TRỌNG (§2 Kiến Trúc Tham Chiếu: DNS/LB, compute, state là 3 lớp riêng):
  1_verify_target    — /v1/state của region phụ: weights? vector count? pool_state?
  2_restore_snapshot — gọi state/snapshot.py get + state/snapshot.py rpo()
                       Log BẮT BUỘC: rpo_seconds, docs_lost, embed_model_version.
                       (§3: "backup index nhưng quên backup embedding model version
                        -> index không tương thích khi restore")
  3_scale_pool       — ghi "full" vào state/region-<t>/pool_state (warm -> full)
  4_wait_ready       — POLL /readyz tới khi 200. Region phụ có WARMUP_SECONDS —
                       đây là GPU pool warm-up của §4, nó nằm trong RTO của bạn.
  5_dns_cutover      — ghi region đích vào edge/active_region

BẪY: nếu bạn đổi edge/active_region TRƯỚC bước 4, user sẽ nhận 503 từ CẢ HAI region
và RTO của bạn dài hơn, không ngắn hơn. Nếu bước 4 timeout -> ABORT, KHÔNG cutover.

Mỗi bước ghi 1 dòng vào reports/failover-events.jsonl với ts + step.
Không có dòng 5_dns_cutover = tools/measure_rto.py không tìm được t_cutover = mất điểm.

Chạy:  python dr/failover.py --target b --backend fs
"""
import argparse
import json
import pathlib
import sys
import time

import httpx

sys.path.insert(0, ".")
from state import snapshot  # noqa: E402

URL = {"a": "http://127.0.0.1:8001", "b": "http://127.0.0.1:8002"}
LOG = pathlib.Path("reports/failover-events.jsonl")


def emit(**kw):
    now = time.time()
    record = {
        "ts": now,
        "iso": time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)
        ),
        **kw,
    }

    LOG.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(record, ensure_ascii=False)

    with LOG.open("a", encoding="utf-8") as stream:
        stream.write(text + "\n")

    print(text, flush=True)
    return record


def state_of(region: str) -> dict:
    response = httpx.get(
        f"{URL[region]}/v1/state",
        timeout=2.0,
    )
    response.raise_for_status()
    return response.json()


def failover(target: str, backend: str, wait: float) -> dict:
    if target not in URL or backend not in {"fs", "minio"} or wait <= 0:
        return {"ok": False, "error": "invalid_arguments"}

    current_step = "1_verify_target"

    try:
        # 1. Kiểm tra trạng thái hiện tại của region đích.
        before = state_of(target)

        emit(
            step=current_step,
            target=target,
            ok=True,
            state=before,
        )

        # 2. Restore snapshot và đo phần dữ liệu thiếu.
        current_step = "2_restore_snapshot"
        restore_started = time.time()
        metadata = snapshot.get(target, backend)

        source = metadata.get(
            "source_region",
            "a" if target == "b" else "b",
        )

        recovery_point = snapshot.rpo(
            pathlib.Path(f"state/region-{source}/vectors.sqlite"),
            pathlib.Path(f"state/region-{target}/vectors.sqlite"),
        )

        emit(
            step=current_step,
            target=target,
            ok=True,
            start_ts=restore_started,
            duration_s=round(time.time() - restore_started, 3),
            snapshot_at=metadata.get("snapshot_at"),
            embed_model_version=metadata.get("embed_model_version"),
            **recovery_point,
        )

        # 3. Chuyển pool sang full.
        current_step = "3_scale_pool"
        target_dir = pathlib.Path(f"state/region-{target}")
        target_dir.mkdir(parents=True, exist_ok=True)

        (target_dir / "pool_state").write_text(
            "full", encoding="utf-8"
        )

        emit(
            step=current_step,
            target=target,
            ok=True,
            pool_state="full",
        )

        # 4. Chờ region đích thực sự ready.
        current_step = "4_wait_ready"
        wait_started = time.monotonic()
        deadline = wait_started + wait
        ready = False

        while time.monotonic() < deadline:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break

            try:
                response = httpx.get(
                    f"{URL[target]}/readyz",
                    timeout=min(2.0, remaining),
                )

                if response.status_code == 200:
                    ready = True
                    break

            except httpx.HTTPError:
                pass

            remaining = deadline - time.monotonic()
            if remaining > 0:
                time.sleep(min(1.0, remaining))

        if not ready:
            emit(
                step=current_step,
                target=target,
                ok=False,
                waited_s=round(
                    time.monotonic() - wait_started, 3
                ),
                error="target_not_ready_before_timeout",
            )

            return {
                "ok": False,
                "target": target,
                "error": "target_not_ready_before_timeout",
            }

        after = state_of(target)

        emit(
            step=current_step,
            target=target,
            ok=True,
            waited_s=round(
                time.monotonic() - wait_started, 3
            ),
            state=after,
        )

        # 5. Chỉ cutover sau khi region đích ready.
        current_step = "5_dns_cutover"
        active = pathlib.Path("edge/active_region")
        active.parent.mkdir(parents=True, exist_ok=True)
        active.write_text(target, encoding="utf-8")

        cutover = emit(
            step=current_step,
            target=target,
            ok=True,
            active_region=target,
        )

        return {
            "ok": True,
            "target": target,
            "state": after,
            "cutover_ts": cutover["ts"],
            "embed_model_version": metadata.get(
                "embed_model_version"
            ),
            **recovery_point,
        }

    except (Exception, SystemExit) as exc:
        error = f"{type(exc).__name__}: {exc}"

        emit(
            step=current_step,
            target=target,
            ok=False,
            error=error,
        )

        return {
            "ok": False,
            "target": target,
            "error": error,
        }


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--target", default="b", choices=["a", "b"])
    p.add_argument("--backend", default="fs", choices=["fs", "minio"])
    p.add_argument("--wait", type=float, default=60)
    a = p.parse_args()
    print(json.dumps(failover(a.target, a.backend, a.wait), indent=2))

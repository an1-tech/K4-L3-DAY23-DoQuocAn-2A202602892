# Runbook — Region chính down

Phạm vi: lab local, primary A, target B, bare mode, backend fs,
chaos netblock --mock. Thực hiện trong Ubuntu tại thư mục gốc bản
sao Linux, với môi trường Python day23 đã kích hoạt.

Trước incident: A ready, B alive, Edge trỏ A; replication phải tạo
được snapshot mới với MANIFEST.json và embedding model version.
Không tấn công nếu replication báo lỗi.

On-call thực hiện; Incident lead quyết định failback.
Trong diễn tập cá nhân, DoQuocAn đảm nhiệm hai vai trò này.

## Checklist vận hành

Chạy runbook ở bước 2 và nhập `y` khi được hỏi. Runbook thực hiện
restore, scale, wait-ready và cutover một lần. Các lệnh kiểm tra
ở bước 3–5 không gọi lại failover.

| # | Bước | Lệnh / thao tác | Biết là xong khi | Ai làm |
|---|---|---|---|---|
| 1 | Xác nhận outage | `python3 chaos/kill_region.py status` | A không phản hồi; health checker ghi A UNHEALTHY sau 3 lỗi liên tiếp; B còn alive | On-call |
| 2 | Mở incident và ghi thời gian | `python3 dr/runbook.py --primary a --target b --backend fs` | Log có `thong_bao_incident`, t_outage và operator_ts; runbook hỏi y/N | On-call |
| 3 | Xác nhận restore sang B | Nhập `y` tại lời nhắc; kiểm tra bằng `tail -n 5 reports/failover-events.jsonl` | `2_restore_snapshot` thành công, có rpo_seconds, docs_lost và embed_model_version | On-call |
| 4 | Scale pool và chờ readiness | `curl -i http://127.0.0.1:8002/readyz` | HTTP 200, ready=true, pool full, vector count > 0; runbook xác nhận weights | Platform operator / on-call |
| 5 | Xác nhận cutover | `curl -s http://127.0.0.1:8080/edge/state` và `curl -s http://127.0.0.1:8080/v1/infer` | Sau cache TTL, active_region=b và inference phục vụ bởi B với HTTP 200 | On-call |
| 6 | Kiểm tra golden signals | `tail -n 2 reports/runbook-run.jsonl` | Bước 6 có 10 request, errors=0, error_rate=0 và p95 < 1000ms | On-call |
| 7 | Đo RTO và tổng kết | `python3 tools/measure_rto.py --loadgen reports/drill-2-withdr.jsonl --target-rto 300` | valid=true, warnings=[], PASS; hoàn thiện rto-evidence.md và postmortem.md | Incident lead |

Ngưỡng p95 < 1000ms là ngưỡng vận hành đề xuất cho mock local.
Code ghi p95; operator đánh giá nó với ngưỡng này.
Lần chạy đã đo: 10 request, 0 lỗi, p95=3.93ms.
Evidence: `reports/runbook-run.jsonl:10`.

`--auto` chỉ dùng trong drill/CI. Vận hành mặc định giữ xác nhận y/N.
`elapsed_s` của runbook không thay thế RTO từ load generator.

## Abort và rollback

- Nếu target không liên hệ được, snapshot lỗi hoặc /readyz không
  đạt trong thời gian chờ: abort, không cutover.
- Nếu đã cutover nhưng B liên tục lỗi hoặc golden signals vượt
  ngưỡng: on-call báo Incident lead, xác minh lại trước khi failback.
- Incident lead có quyền cho phép failback; on-call thực hiện.
  Không tự động đổi qua lại A/B.
- Chỉ trả traffic về A khi A ready qua nhiều probe liên tiếp,
  weights và embedding version hợp lệ, dữ liệu A/B đã được đối soát
  và snapshot dùng cho failback đã được duyệt.
- A sống lại chưa đủ điều kiện trả traffic về A. Nếu B đã nhận ghi
  mới, phải xử lý chênh lệch dữ liệu trước.

Với netblock bare mode, lệnh khôi phục process A:

```bash
python3 chaos/kill_region.py restore --region a --backend bare
```

Sau đó kiểm tra A:

```bash
curl -i http://127.0.0.1:8001/readyz
curl -s http://127.0.0.1:8001/v1/state
```

Các lệnh này chỉ khôi phục và kiểm tra A, chưa chuyển traffic.
Sau khi dữ liệu và snapshot failback được duyệt, có thể dùng
`python3 dr/failover.py --target a --backend fs`; không chạy lệnh
này để thử trong lúc đang thu bằng chứng Drill 2.

## Lưu bằng chứng và kết thúc

Giữ các log chaos, loadgen, health, failover, replication, runbook
và hai file JSON đo kết quả. Không chạy make clean trước khi lưu
bộ bằng chứng.

Dừng stack, xử lý CRLF trong luồng đưa vào Bash mà không sửa script:

```bash
bash -c "$(tr -d '\r' < scripts/down_bare.sh)" scripts/down_bare.sh
```
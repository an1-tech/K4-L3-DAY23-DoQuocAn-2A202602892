# RTO/RPO Evidence — Lab 23

Sinh viên: DoQuocAn — 2A202602892.

Môi trường diễn tập: Ubuntu/WSL, bare mode, chaos `netblock --mock`,
snapshot backend `fs`. Lần đo thành công chạy trong filesystem Linux.

Timestamp trình bày theo UTC. Các mốc của load generator là thời điểm
bắt đầu request được ghi trong trường `ts`, không phải thời điểm nhận
xong response.

## 1. Drill 1 — không có DR

| Chỉ số | Giá trị | Cách đo | Evidence |
|---|---|---|---|
| t_outage | 2026-10-09T05:12:13.612+00:00 | Timestamp của kill event | `chaos/chaos-events.jsonl:1` |
| Request fail đầu tiên | +0.170s | Request `ok:false` đầu tiên sau outage | `reports/drill-1-nodr.jsonl:17` |
| Số request thất bại sau outage | 16 | Kết quả công cụ đo | `reports/measure-drill-1.json` |
| Phục hồi trong cửa sổ diễn tập | Không có | Không tìm thấy request phục hồi sau lỗi | `reports/measure-drill-1.json` |
| RTO | NO_RECOVERY | Không đo được thời điểm phục hồi | `reports/measure-drill-1.json` |

Baseline chứng minh hệ thống không tự phục hồi trong cửa sổ traffic
40 giây. `NO_RECOVERY` không phải một giá trị RTO hữu hạn và không có
nghĩa là đã đo được thời gian mất dịch vụ vô hạn.

## 2. Drill 2 — có DR

| Mốc | Giây từ t_outage | Cách đo | Evidence |
|---|---:|---|---|
| t_outage | 0.000s | Kill A lúc 2026-10-09T05:32:53.208+00:00 | `chaos/chaos-events.jsonl:5` |
| Request fail đầu tiên | 2.005s | Request `ok:false` đầu tiên sau outage | `reports/drill-2-withdr.jsonl:26` |
| Health checker phát hiện A | 14.981s | A chuyển sang UNHEALTHY sau 3 lỗi liên tiếp | `reports/health-events.jsonl:4` |
| Verify target | 15.128s | B được kiểm tra trước restore | `reports/failover-events.jsonl:7` |
| Snapshot restore hoàn tất | 15.131s | Bước `2_restore_snapshot` thành công | `reports/failover-events.jsonl:8` |
| Scale pool B thành full | 15.131s | Bước `3_scale_pool` | `reports/failover-events.jsonl:9` |
| B ready | 21.228s | Bước `4_wait_ready` thành công | `reports/failover-events.jsonl:10` |
| DNS/LB cutover | 21.229s | Bước `5_dns_cutover`, active region B | `reports/failover-events.jsonl:11` |
| Request phục hồi qua Edge | 22.172s | Request `ok:true` đầu tiên sau lỗi, phục vụ bởi B | `reports/drill-2-withdr.jsonl:36` |

| Chỉ số | Đo được | Mục tiêu | Verdict / Evidence |
|---|---|---|---|
| RTO — Inference API | 22.2s; từ timestamp là 22.172s | 300s | PASS — `reports/measure-drill-2.json` |
| RPO — Vector DB tại restore | 2.0s / 1 document | 300s | Đạt mục tiêu thời gian — `reports/failover-events.jsonl:8` |
| Request thất bại sau outage | 10 | Ghi nhận ảnh hưởng | `reports/measure-drill-2.json` |
| Tính hợp lệ | valid=true; warnings=[] | Drill hợp lệ | `reports/measure-drill-2.json` |
| Region phục hồi | B | Khác region A bị tấn công | `reports/drill-2-withdr.jsonl:36` |

Snapshot đã restore có embedding model version
`embed-model=vi-e5-base@v3`. B có 291 document, weights tồn tại và
pool `full` khi ready.
Evidence: `reports/failover-events.jsonl:8`,
`reports/failover-events.jsonl:10`.

## 3. Phân rã RTO

Cấu hình health checker: interval=5s, threshold=3.
Detection floor danh nghĩa theo bài lab là 5 × 3 = 15.000s.
Thời gian detection thực đo là 14.981s; khác biệt nhỏ phụ thuộc pha
polling và thời gian thực hiện probe.
Evidence: `reports/health-events.jsonl:4`.

| Thành phần | Giây | Cách đo / Evidence | Cách giảm |
|---|---:|---|---|
| Health-check detection thực đo | 14.981 | t_detect − t_outage; cấu hình floor danh nghĩa 15s — `reports/health-events.jsonl:4`, `chaos/chaos-events.jsonl:5` | Thử interval nhỏ hơn, giữ threshold và đo nguy cơ flapping |
| Snapshot restore | 0.002 | Trường `duration_s` — `reports/failover-events.jsonl:8` | Chuẩn bị snapshot hợp lệ và kiểm tra restore trước drill |
| Chờ ready / GPU pool warm-up mô phỏng | 6.097 | Trường `waited_s` — `reports/failover-events.jsonl:10` | Giữ pool dự phòng ở trạng thái sẵn sàng hơn, đánh đổi tài nguyên |
| Sau cutover đến request phục hồi | 0.944 | t_recovered − t_cutover — `reports/drill-2-withdr.jsonl:36`, `reports/failover-events.jsonl:11` | Giảm TTL, đo lại với cùng traffic |
| Điều phối và các thao tác còn lại | 0.148 | Phần dư sau khi trừ bốn khoản trên; gồm xác nhận, verify target, scale và ghi log — `reports/runbook-run.jsonl:6`, `reports/failover-events.jsonl:7` | Giảm thao tác thừa và giữ failover chỉ gọi một lần |
| Tổng | 22.172 | 14.981 + 0.002 + 6.097 + 0.944 + 0.148 | Khớp RTO từ timestamp |

Khoản 0.944s gồm ảnh hưởng cache của Edge và thời điểm request tiếp
theo quan sát được phục hồi; không phải phép đo riêng DNS TTL.

## 4. Giới hạn của phép đo

RPO 2.0s / 1 document là chênh lệch dữ liệu tại thời điểm restore,
được ghi ở `reports/failover-events.jsonl:8`.

Trong mô hình này, ingest và replication là các process riêng,
tiếp tục truy cập filesystem khi process serving A bị SIGSTOP.
Vì vậy, kết quả không chứng minh document đã bị mất vĩnh viễn.
Không tính lại RPO bằng DB cuối diễn tập để thay thế số tại restore.

Mười request golden signals đi trực tiếp tới B: error rate=0,
p95=3.93ms. Đây là kiểm tra target, còn RTO người dùng được đo qua
Edge bằng load generator.
Evidence: `reports/runbook-run.jsonl:10`.
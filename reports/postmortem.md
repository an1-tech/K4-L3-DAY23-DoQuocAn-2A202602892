# Postmortem — DR Drill Lab 23

Sinh viên: DoQuocAn — 2A202602892.
Ngày diễn tập: 2026-10-09.
Timestamp bên dưới dùng UTC; giờ địa phương Asia/Bangkok là UTC+7.

Diễn tập gây outage serving A bằng netblock --mock, dùng SIGSTOP.
Hệ thống phục hồi inference qua B sau 22.172s, công cụ làm tròn
thành 22.2s. RPO tại restore là 2.0s / 1 document.

## 1. Timeline

| ISO time UTC | Sự kiện | Evidence |
|---|---|---|
| 2026-10-09T05:32:53.208+00:00 | Outage A bắt đầu | `chaos/chaos-events.jsonl:5` |
| 2026-10-09T05:32:55.213+00:00 | Request lỗi đầu tiên bắt đầu, +2.005s | `reports/drill-2-withdr.jsonl:26` |
| 2026-10-09T05:33:08.190+00:00 | Checker phát hiện A UNHEALTHY, +14.981s | `reports/health-events.jsonl:4` |
| 2026-10-09T05:33:08.330+00:00 | Runbook mở incident, +15.121s | `reports/runbook-run.jsonl:6` |
| 2026-10-09T05:33:08.330+00:00 | Xác nhận bằng chế độ --auto, +15.122s | Trường operator_confirm_ts tại `reports/runbook-run.jsonl:7` |
| 2026-10-09T05:33:08.339+00:00 | Restore snapshot thành công, +15.131s | `reports/failover-events.jsonl:8` |
| 2026-10-09T05:33:14.436+00:00 | B ready, +21.228s | `reports/failover-events.jsonl:10` |
| 2026-10-09T05:33:14.437+00:00 | Cutover sang B, +21.229s | `reports/failover-events.jsonl:11` |
| 2026-10-09T05:33:14.471+00:00 | Golden signals: 10 request, 0 lỗi, p95=3.93ms | `reports/runbook-run.jsonl:10` |
| 2026-10-09T05:33:15.380+00:00 | Request phục hồi qua Edge bắt đầu, +22.172s | `reports/drill-2-withdr.jsonl:36` |

Xác nhận trong lần chạy này là --auto, không phải một thao tác
nhập y của người vận hành. Timestamp xác nhận nằm trong trường
operator_confirm_ts; không dùng timestamp lúc bước 3 ghi log
để thay thế thời điểm xác nhận.

## 2. RTO/RPO so với mục tiêu và gap analysis

Quy ước: gap = số đo − mục tiêu. Gap âm nghĩa là đạt mục tiêu.

| Chỉ số | Mục tiêu | Đo được | Gap | Evidence |
|---|---:|---:|---:|---|
| RTO | 300s | 22.2s | -277.8s | `reports/measure-drill-2.json` |
| RPO tại restore | 300s | 2.0s / 1 document | -298.0s về thời gian | `reports/failover-events.jsonl:8` |

RTO chính xác từ timestamp là 22.172s.
Drill hợp lệ, warnings rỗng, phục hồi bởi B, có 10 request thất bại
sau outage.
Evidence: `reports/measure-drill-2.json`.

Phân rã RTO:
- Detection thực đo: 14.981s.
- Snapshot restore: 0.002s.
- Chờ ready / warm-up mô phỏng: 6.097s.
- Sau cutover đến request phục hồi: 0.944s.
- Điều phối và các thao tác còn lại: 0.148s.
- Tổng: 22.172s.

Detection là phần lớn nhất, chiếm khoảng 67.57% RTO chính xác.
Evidence: `reports/health-events.jsonl:4`,
`reports/failover-events.jsonl:8`,
`reports/failover-events.jsonl:10`,
`reports/failover-events.jsonl:11`,
`reports/drill-2-withdr.jsonl:36`.

Không thể đánh giá RTO chỉ bằng timestamp cutover: request qua
Edge phục hồi sau cutover thêm 0.944s.

## 3. Root cause — 5 whys

1. Vì sao inference qua Edge bị lỗi?
   Process serving A bị SIGSTOP nên không trả lời upstream request.

2. Vì sao Edge chưa phục vụ ngay từ B?
   Pointer còn trỏ A; failover phải chờ xác nhận outage và chuẩn bị
   target an toàn trước khi cutover.

3. Vì sao không cutover ngay khi probe đầu tiên thất bại?
   Checker dùng 3 lỗi liên tiếp để tránh coi một lỗi thoáng qua
   là outage. Đây là đánh đổi giữa tốc độ phát hiện và flapping.
   Evidence: reports/health-events.jsonl:4.

4. Vì sao B cần thêm thời gian sau detection?
   B ban đầu warm, DB rỗng và thiếu weights. Failover restore
   snapshot, scale full rồi chờ readiness.
   Evidence: reports/failover-events.jsonl:7,
   reports/failover-events.jsonl:8,
   reports/failover-events.jsonl:10.

5. Vì sao người dùng phục hồi sau timestamp cutover?
   Edge có cache TTL; load generator quan sát phục hồi ở request
   tiếp theo. Pointer được ghi chưa có nghĩa mọi request đã dùng B.
   Evidence: reports/failover-events.jsonl:11,
   reports/drill-2-withdr.jsonl:36.

Nguyên nhân kéo dài gián đoạn là thời gian detection và trạng thái
warm standby của target trong mô hình active-passive.
Không quy lỗi cho người vận hành.

Baseline không có DR đã không phục hồi trong cửa sổ đo:
reports/measure-drill-1.json. Drill có DR phục hồi theo đúng thứ tự
verify → restore → scale → ready → cutover.

Health checker chạy bằng process riêng, không import serving.
Nếu monitor nằm trong chính process serving thì khi process chết,
monitor cũng không thể tiếp tục phát hiện và báo outage.

## 4. Action items

Các mục dưới đây là kế hoạch cải tiến đề xuất, chưa phải kết quả
đã đo hoặc thay đổi đã triển khai.

| # | Action item | Owner | Deadline | Tác động dự kiến |
|---|---|---|---|---|
| 1 | Thử interval=1s, giữ threshold=3; chạy lại drill và kiểm tra lỗi thoáng qua | DoQuocAn — on-call | 2026-10-12 | Nominal detection floor giảm từ 15s xuống 3s; mức giảm RTO phải đo lại |
| 2 | Giữ bước kiểm tra replication mới thành công trước chaos; chạy lab trên filesystem Linux | DoQuocAn — platform operator | 2026-10-13 | Ngăn tấn công khi snapshot chưa hợp lệ; giảm nguy cơ failover không phục hồi |
| 3 | So sánh replication every=10s với 30s qua nhiều lần chạy | DoQuocAn — data operator | 2026-10-16 | Đánh giá phân bố RPO; đánh đổi thêm I/O và số lần snapshot |

## 5. Ba câu hỏi bắt buộc

### 1. interval × threshold là bao nhiêu và chiếm bao nhiêu RTO?

5s × 3 = 15s theo mô hình danh nghĩa của bài.
15 / 22.172 × 100 ≈ 67.65%.

Detection thực đo là 14.981s, chiếm khoảng 67.57%.
Hai tỷ lệ khác nhau vì một giá trị là danh nghĩa, một giá trị
lấy từ timestamp thực.

### 2. Nếu hạ interval xuống 1s, giảm bao nhiêu và đánh đổi gì?

Với threshold=3, nominal floor từ 15s xuống 3s, chênh 12s.
Đây là ước tính từ cấu hình, chưa chứng minh RTO sẽ giảm đúng 12s.

Probe chạy thường xuyên hơn và 3 lỗi có thể tập trung trong cửa sổ
ngắn hơn, tăng khả năng phản ứng với lỗi thoáng qua. Cần kiểm tra
flapping và đo lại RTO bằng traffic thật.

### 3. Outage 6 giờ và A mất dữ liệu vĩnh viễn thì docs_lost có nghĩa gì?

Tại restore, B thiếu 1 document so với A, tương ứng khoảng dữ liệu
2.0s. Nếu A và mọi nguồn tái tạo đều mất vĩnh viễn, phần chưa được
replica có thể không thể khôi phục, gây thiếu dữ liệu cho khách hàng.

Trong lab, ingest và replication vẫn truy cập filesystem khi
serving A bị SIGSTOP. Vì vậy docs_lost=1 chỉ là chênh lệch tại
restore, không phải bằng chứng 1 document đã bị xóa vĩnh viễn.
Evidence: reports/failover-events.jsonl:8.

Khi cần kiểm chứng mục tiêu RTO 5 phút, mở measure-drill-2.json và
truy ngược tới kill event cùng request phục hồi qua Edge.
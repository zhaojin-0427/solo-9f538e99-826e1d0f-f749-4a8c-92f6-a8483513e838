"""Flask 网页与查询 API。

网页展示与 API 使用同一个 compute_hour_result()，保证两边结果一致；
小时雨量由 readings 表现算现算，补传/修订整点读数提交后，
前后两小时立即同时反映新值，不存在缓存不一致。
"""
from __future__ import annotations

import os

from flask import (
    Flask, flash, g, jsonify, redirect, render_template, request, url_for,
)

from . import core, db


def create_app(db_path: str | None = None) -> Flask:
    app = Flask(__name__)
    app.config["DB_PATH"] = db_path or os.environ.get("RAIN_DB", "rainfall.db")
    app.config["MAX_CONTENT_LENGTH"] = 16 * 1024 * 1024  # 上传上限 16MB
    app.secret_key = os.environ.get("SECRET_KEY", os.urandom(32))

    # 启动时建库建表（幂等），保证重启后可直接查询
    conn = db.connect(app.config["DB_PATH"])
    try:
        db.init_db(conn)
    finally:
        conn.close()

    def get_conn() -> db.sqlite3.Connection:
        if "db_conn" not in g:
            g.db_conn = db.connect(app.config["DB_PATH"])
        return g.db_conn

    @app.teardown_appcontext
    def _close_conn(exc):
        conn = g.pop("db_conn", None)
        if conn is not None:
            conn.close()

    @app.template_filter("iso")
    def _iso_filter(epoch):
        return core.iso_utc(int(epoch))

    def compute_hour_result(station: str, hour_epoch: int) -> dict:
        """网页与 API 共用的小时计算入口。"""
        counters = db.fetch_hour_counters(get_conn(), station, hour_epoch)
        return core.compute_hour(counters, station, hour_epoch)

    def compute_lag_result(up_station: str, down_station: str,
                           start_epoch: int, end_epoch: int, max_lag: int) -> dict:
        """网页与 API 共用的时差分析入口。

        两站读数在同一个只读快照事务中取出，保证补传与查询交错时，
        两站及全部候选时差都取自同一已提交状态的有效读数。
        """
        # 两站都只需窗口内各整点小时的雨量（计算到 window_end 那一小时，
        # 端点覆盖到 end+1h）；下游延后配对的最大小时键恰为 window_end
        endpoint_end = end_epoch + core.HOUR_SECONDS
        with db.snapshot_read(get_conn()) as conn:
            up_counters = db.fetch_range_counters(conn, up_station, start_epoch, endpoint_end)
            down_counters = db.fetch_range_counters(conn, down_station, start_epoch, endpoint_end)
        up_rain = core.build_hour_rainfall(up_counters, up_station, start_epoch, end_epoch)
        down_rain = core.build_hour_rainfall(down_counters, down_station, start_epoch, end_epoch)
        result = core.analyze_lag(up_rain, down_rain, start_epoch, end_epoch, max_lag)
        result["upstream_station"] = up_station
        result["downstream_station"] = down_station
        result["conclusion_text"] = core.lag_conclusion_text(result)
        return result

    def parse_lag_params(args):
        """解析并校验时差分析参数，返回 (up, down, start_epoch, end_epoch, max_lag)。"""
        up = args.get("upstream", "").strip()
        down = args.get("downstream", "").strip()
        if not up or not down:
            raise ValueError("请选择上游站与下游站")
        if up == down:
            raise ValueError("上游站与下游站必须不同")
        start = core.parse_hour(args.get("start", ""))
        end = core.parse_hour(args.get("end", ""))
        if end < start:
            raise ValueError("结束整点不能早于起始整点")
        try:
            max_lag = int(args.get("max_lag", "").strip(), 10)
        except ValueError:
            raise ValueError(f"最大时差必须为 1~{core.MAX_LAG_LIMIT} 的整数") from None
        if not 1 <= max_lag <= core.MAX_LAG_LIMIT:
            raise ValueError(f"最大时差必须在 1~{core.MAX_LAG_LIMIT} 小时之间")
        return up, down, start, end, max_lag

    def read_upload_text():
        """从 multipart 文件或文本框读取 CSV 文本，返回 (text, filename)。"""
        file = request.files.get("file")
        if file is not None and file.filename:
            try:
                return file.read().decode("utf-8-sig"), file.filename
            except UnicodeDecodeError:
                raise core.CsvFormatError("文件不是有效的 UTF-8 编码")
        text = request.form.get("csv_text", "")
        if text.strip():
            return text, None
        raise core.CsvFormatError("请选择 CSV 文件或粘贴 CSV 文本")

    # ---------------- 网页 ----------------

    @app.get("/")
    def index():
        conn = get_conn()
        return render_template(
            "index.html",
            stations=db.list_stations(conn),
            batches=db.list_batches(conn),
        )

    @app.post("/upload")
    def upload_html():
        try:
            text, filename = read_upload_text()
            valid, errors = core.parse_csv_text(text)
        except core.CsvFormatError as exc:
            flash(f"上传失败：{exc}", "error")
            return redirect(url_for("index"))
        report = db.apply_batch(get_conn(), valid, errors, filename)
        return render_template("batch.html", report=report)

    @app.get("/report")
    def report_html():
        station = request.args.get("station", "").strip()
        hour_text = request.args.get("hour", "").strip()
        if not station or not hour_text:
            flash("请填写站点和整点小时", "error")
            return redirect(url_for("index"))
        try:
            hour = core.parse_hour(hour_text)
        except ValueError as exc:
            flash(f"小时参数无效：{exc}", "error")
            return redirect(url_for("index"))
        result = compute_hour_result(station, hour)
        return render_template(
            "report.html",
            r=result,
            prev_hour=core.iso_utc(hour - core.HOUR_SECONDS),
            next_hour=core.iso_utc(hour + core.HOUR_SECONDS),
        )

    @app.get("/batches/<int:batch_id>")
    def batch_detail(batch_id):
        batch, rows = db.get_batch(get_conn(), batch_id)
        if batch is None:
            flash("批次不存在", "error")
            return redirect(url_for("index"))
        return render_template("batch_detail.html", batch=batch, rows=rows)

    @app.get("/lag")
    def lag_html():
        conn = get_conn()
        stations = [r["station"] for r in db.list_stations(conn)]
        params = {
            "upstream": request.args.get("upstream", ""),
            "downstream": request.args.get("downstream", ""),
            "start": request.args.get("start", ""),
            "end": request.args.get("end", ""),
            "max_lag": request.args.get("max_lag", "3"),
        }
        # 没有任何查询参数时只展示表单
        if not any(request.args.get(k) for k in ("upstream", "downstream", "start", "end", "max_lag")):
            return render_template("lag.html", stations=stations, params=params, result=None)
        try:
            up, down, start, end, max_lag = parse_lag_params(request.args)
        except ValueError as exc:
            flash(f"查询参数无效：{exc}", "error")
            return render_template("lag.html", stations=stations, params=params, result=None), 400
        result = compute_lag_result(up, down, start, end, max_lag)
        return render_template("lag.html", stations=stations, params=params, result=result)

    # ---------------- API（与网页同一计算入口，结果一致） ----------------

    @app.post("/api/upload")
    def upload_api():
        try:
            text, filename = read_upload_text()
            valid, errors = core.parse_csv_text(text)
        except core.CsvFormatError as exc:
            return jsonify({"error": str(exc)}), 400
        report = db.apply_batch(get_conn(), valid, errors, filename)
        return jsonify(_report_json(report))

    @app.get("/api/hourly")
    def hourly_api():
        station = request.args.get("station", "").strip()
        if not station:
            return jsonify({"error": "缺少 station 参数"}), 400
        try:
            hour = core.parse_hour(request.args.get("hour", ""))
        except ValueError as exc:
            return jsonify({"error": f"hour 参数无效: {exc}"}), 400
        return jsonify(compute_hour_result(station, hour))

    @app.get("/api/lag")
    def lag_api():
        """上下游时差（候选提前量）分析，与网页 /lag 同一计算入口，结果一致。"""
        try:
            up, down, start, end, max_lag = parse_lag_params(request.args)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        return jsonify(compute_lag_result(up, down, start, end, max_lag))

    @app.get("/api/stations")
    def stations_api():
        out = [{
            "station": r["station"],
            "readings": r["n"],
            "first_minute": core.iso_utc(r["first_minute"]),
            "last_minute": core.iso_utc(r["last_minute"]),
        } for r in db.list_stations(get_conn())]
        return jsonify(out)

    @app.get("/api/batches")
    def batches_api():
        return jsonify([dict(r) for r in db.list_batches(get_conn(), limit=100)])

    @app.get("/api/health")
    def health():
        get_conn().execute("SELECT 1")
        return jsonify({"status": "ok"})

    @app.errorhandler(413)
    def too_large(_):
        return "上传文件过大（上限 16MB）", 413

    return app


def _report_json(report: dict) -> dict:
    return {
        "batch_id": report["batch_id"],
        "filename": report["filename"],
        "uploaded_at": report["uploaded_at"],
        "total_rows": report["total_rows"],
        "accepted_count": report["accepted_count"],
        "rejected_count": report["rejected_count"],
        "accepted": [{
            "row_no": a["row_no"],
            "action": a["action"],
            "station": a["reading"].station,
            "device": a["reading"].device,
            "minute": core.iso_utc(a["reading"].minute_epoch),
            "counter": a["reading"].counter,
            "revision": a["reading"].revision,
        } for a in report["accepted"]],
        "rejected": report["rejected"],
    }


def main():
    app = create_app()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "8000")), threaded=True)


if __name__ == "__main__":
    main()

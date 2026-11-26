"""Flask application factory and JSON API."""
from __future__ import annotations

import os
from typing import Dict, Optional

from flask import Flask, jsonify, render_template, request

from .engine import BisectSession
from .fixture import ensure_builtin_repo
from .history import PRESETS, load_graph, validate_pair
from .runner import GitArchiveRunner, GlobalSlotGate
from .scheduler import SlotScheduler


def create_app(test_config: Optional[dict] = None) -> Flask:
    app = Flask(
        __name__,
        template_folder="templates",
        static_folder="static",
    )
    app.config.update(
        HISTORY_REPO=os.environ.get("HISTORY_REPO", "/tmp/gsb-bisect/history-repo"),
        WORK_ROOT=os.environ.get("WORK_ROOT", "/tmp/gsb-bisect/work"),
        DELAY_MIN=float(os.environ.get("TEST_DELAY_MIN", "0.5")),
        DELAY_MAX=float(os.environ.get("TEST_DELAY_MAX", "1.4")),
        AUTO_BUILD_FIXTURE=True,
        TESTING=False,
    )
    if test_config:
        app.config.update(test_config)

    # Graph / runner can be injected by tests.
    graph = app.config.get("GRAPH")
    runner = app.config.get("RUNNER")
    if graph is None:
        repo = app.config["HISTORY_REPO"]
        if app.config["AUTO_BUILD_FIXTURE"]:
            ensure_builtin_repo(repo)
        graph = load_graph(repo)
        app.config["GRAPH"] = graph
    if runner is None:
        runner = GitArchiveRunner(
            app.config["HISTORY_REPO"],
            app.config["WORK_ROOT"],
            delay_min=app.config["DELAY_MIN"],
            delay_max=app.config["DELAY_MAX"],
        )
    # Process-wide guarantee: never more than two real tests at once.
    # The shared FIFO scheduler is the authoritative admission point (it
    # distinguishes "queued for a slot" from "actually running"); the
    # GlobalSlotGate stays as a defence-in-depth hard cap inside the runner.
    runner = GlobalSlotGate(runner, slots=2)

    scheduler = app.config.get("SCHEDULER") or app.config.get("SLOT_SCHEDULER")
    if scheduler is None:
        scheduler = SlotScheduler(slots=2)
    app.config["SCHEDULER"] = scheduler

    sessions: Dict[str, BisectSession] = {}

    # ---------------------------------------------------------------- pages
    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/api/health")
    def health():
        return jsonify(ok=True, sessions=len(sessions),
                       slot_pool=scheduler.stats())

    @app.get("/api/history")
    def history():
        return jsonify(
            commits=graph.to_public(),
            presets=PRESETS,
            order=list(graph.order),
        )

    # ------------------------------------------------------------- sessions
    @app.post("/api/sessions")
    def create_session():
        data = request.get_json(silent=True) or request.form
        bad_ref = str(data.get("bad") or "").strip()
        good_ref = str(data.get("good") or "").strip()
        bad, good, err = validate_pair(graph, bad_ref, good_ref)
        if err:
            return jsonify(error=err), 400

        session = BisectSession(graph, bad, good, runner, scheduler=scheduler)
        sessions[session.id] = session
        session.start()
        return jsonify(session.snapshot()), 201

    @app.get("/api/sessions/<sid>")
    def get_session(sid: str):
        session = sessions.get(sid)
        if session is None:
            return jsonify(error="session not found"), 404
        return jsonify(session.snapshot())

    @app.post("/api/sessions/<sid>/retry")
    def retry_session(sid: str):
        session = sessions.get(sid)
        if session is None:
            return jsonify(error="session not found"), 404
        data = request.get_json(silent=True) or {}
        sha = graph.resolve(str(data.get("sha") or "").strip())
        if not sha:
            return jsonify(error="unknown commit"), 400
        ok, msg, _ = session.retry(sha)
        if not ok:
            return jsonify(error=msg), 409
        return jsonify(session.snapshot())

    @app.errorhandler(404)
    def not_found(_e):
        if request.path.startswith("/api/"):
            return jsonify(error="not found"), 404
        return "Not found", 404

    @app.errorhandler(500)
    def server_error(e):  # pragma: no cover
        return jsonify(error=f"server error: {e}"), 500

    return app

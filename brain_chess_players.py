import json
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from typing import Any

import brain_util as bu


COLUMNS: dict[str, int] = {
    "a": 0, "b": 1, "c": 2, "d": 3, "e": 4, "f": 5, "g": 6, "h": 7,
}
ROWS: dict[str, int] = {
    "1": 7, "2": 6, "3": 5, "4": 4, "5": 3, "6": 2, "7": 1, "8": 0,
}
AGENT_COLORS: dict[str, str] = {
    "tactics": "#f97316",
    "positional": "#a3e635",
    "attacker": "#ff4455",
    "defender": "#4a9eff",
    "arbiter": "#c084fc",
}


@dataclass(frozen=True, slots=True)
class ChessConfig:
    region: str = bu.SENTINEL
    scale: float = 1.0
    grid_size: int = 8
    grid_color: str = "rgba(0,255,200,0.95)"
    grid_stroke_width: int = 4
    ready_poll_interval: float = 0.5
    ready_poll_max: int = 60
    post_ready_delay: float = 1.0
    vlm_image_semaphore_count: int = 2
    vlm_text_semaphore_count: int = 2


TACTICS_SYSTEM: str = """\
You are a chess engine analyzing a board screenshot. White pieces are at the bottom. Grid overlay labels columns a-h left to right, rows 1-8 bottom to top.
Find the best tactical move for White: check, capture, or fork.
Output ONLY two squares separated by a space. Example: e2 e4
If no tactical move exists output NONE.\
"""

POSITIONAL_SYSTEM: str = """\
You are a chess engine analyzing a board screenshot. White pieces are at the bottom. Grid overlay labels columns a-h left to right, rows 1-8 bottom to top.
Find the best positional move for White: center control, piece development, or piece activity.
Output ONLY two squares separated by a space. Example: d2 d4
If no good positional move exists output NONE.\
"""

ATTACKER_SYSTEM: str = """\
You are a chess engine analyzing a board screenshot. White pieces are at the bottom. Grid overlay labels columns a-h left to right, rows 1-8 bottom to top.
Find the most aggressive move for White: king attack, sacrifice, or pawn push.
Output ONLY two squares separated by a space. Example: f3 g5
If no aggressive move exists output NONE.\
"""

DEFENDER_SYSTEM: str = """\
You are a chess engine analyzing a board screenshot. White pieces are at the bottom. Grid overlay labels columns a-h left to right, rows 1-8 bottom to top.
Find the best defensive move for White: block a threat, improve king safety, or retreat a piece.
Output ONLY two squares separated by a space. Example: g1 f3
If no defensive move is needed output NONE.\
"""

PARSER_SYSTEM: str = """\
You are a strict text parser. You receive chess analysis text that may contain a move.
Extract exactly one move as two squares separated by a space. Columns are a-h, rows are 1-8.
Output ONLY the two squares. Example: e2 e4
If no valid move is found output NONE.\
"""

ARBITER_SYSTEM: str = """\
You are a chess arbiter analyzing a board screenshot. White pieces are at the bottom. Grid overlay labels columns a-h left to right, rows 1-8 bottom to top.
The image shows colored arrows representing move proposals from different analysts.
Pick the single best move for White from the proposed arrows.
Output ONLY two squares separated by a space representing the chosen move. Example: e2 e4\
"""

PLAYERS: list[tuple[str, str, str]] = [
    ("tactics", AGENT_COLORS["tactics"], TACTICS_SYSTEM),
    ("positional", AGENT_COLORS["positional"], POSITIONAL_SYSTEM),
    ("attacker", AGENT_COLORS["attacker"], ATTACKER_SYSTEM),
    ("defender", AGENT_COLORS["defender"], DEFENDER_SYSTEM),
]


def _col_row_to_notation(col: int, row: int) -> str:
    return f"{chr(ord('a') + col)}{8 - row}"


def _parse_squares(text: str) -> tuple[int, int, int, int] | None:
    tokens: list[str] = text.lower().split()
    squares: list[tuple[int, int]] = []
    for token in tokens:
        clean: str = token.strip(".,!?:;()")
        if len(clean) == 2 and clean[0] in COLUMNS and clean[1] in ROWS:
            squares.append((COLUMNS[clean[0]], ROWS[clean[1]]))
        if len(squares) == 2:
            return squares[0][0], squares[0][1], squares[1][0], squares[1][1]
    return None


@dataclass(frozen=True, slots=True)
class Proposal:
    player: str
    color: str
    from_col: int
    from_row: int
    to_col: int
    to_row: int
    notation: str


def _player_cycle(
    name: str,
    color: str,
    system_prompt: str,
    cfg: ChessConfig,
    image_b64: str,
    grid_overlays: list[dict[str, Any]],
    image_sem: threading.Semaphore,
    text_sem: threading.Semaphore,
) -> Proposal | None:
    bu.ui_status(name, "annotating")

    annotated_b64: str = bu.annotate(name, image_b64, grid_overlays)
    if annotated_b64 == bu.SENTINEL:
        annotated_b64 = image_b64

    user_text: str = "Analyze this position and suggest your move for White."

    vlm_request: dict[str, Any] = bu.make_vlm_request_with_image(
        system_prompt, annotated_b64, user_text,
    )
    bu.ui_status(name, "thinking")
    with image_sem:
        raw_reply: str = bu.vlm_text(name, vlm_request)

    # bu.ui_vlm_cycle(
    #     name,
    #     system_prompt=system_prompt,
    #     user_message=user_text,
    #     raw_image_b64=image_b64,
    #     annotated_image_b64=annotated_b64,
    #     vlm_reply=raw_reply,
    #     overlays=grid_overlays,
    # )

    print(f"  {name} raw: {raw_reply!r}")

    parser_request: dict[str, Any] = bu.make_vlm_request(
        PARSER_SYSTEM, raw_reply,
    )
    with text_sem:
        parsed: str = bu.vlm_text(name, parser_request)

    # bu.ui_vlm_cycle(
    #     name,
    #     system_prompt=PARSER_SYSTEM,
    #     user_message=raw_reply,
    #     raw_image_b64=bu.SENTINEL,
    #     annotated_image_b64=bu.SENTINEL,
    #     vlm_reply=parsed,
    # )

    print(f"  {name} parsed: {parsed!r}")

    move: tuple[int, int, int, int] | None = _parse_squares(parsed)
    if move is None:
        bu.ui_status(name, "no move")
        return None

    n1: str = _col_row_to_notation(move[0], move[1])
    n2: str = _col_row_to_notation(move[2], move[3])
    notation: str = f"{n1}{n2}"

    arrow: list[dict[str, Any]] = bu.make_arrow_overlay(
        move[0], move[1], move[2], move[3],
        color, cfg.grid_size, label=f"{name}: {notation}",
    )
    arrow_b64: str = bu.annotate(name, annotated_b64, arrow)
    # if arrow_b64 != bu.SENTINEL:
    #     bu.ui_vlm_cycle(
    #         name,
    #         system_prompt="",
    #         user_message=f"Proposed: {notation}",
    #         raw_image_b64=annotated_b64,
    #         annotated_image_b64=arrow_b64,
    #         vlm_reply=f"{name} proposes {n1} -> {n2}",
    #         overlays=arrow,
    #     )

    bu.ui_status(name, f"proposes {notation}")
    print(f"  {name}: proposes {n1} -> {n2}")
    return Proposal(
        player=name, color=color,
        from_col=move[0], from_row=move[1],
        to_col=move[2], to_row=move[3],
        notation=notation,
    )


def _arbiter_decide(
    cfg: ChessConfig,
    proposals: list[Proposal],
    base_image_b64: str,
    grid_overlays: list[dict[str, Any]],
    image_sem: threading.Semaphore,
) -> Proposal | None:
    agent: str = "arbiter"
    bu.ui_status(agent, f"evaluating {len(proposals)} proposals")

    all_overlays: list[dict[str, Any]] = list(grid_overlays)
    for p in proposals:
        arrows: list[dict[str, Any]] = bu.make_arrow_overlay(
            p.from_col, p.from_row, p.to_col, p.to_row,
            p.color, cfg.grid_size, label=f"{p.player}: {p.notation}",
        )
        all_overlays.extend(arrows)

    composite_b64: str = bu.annotate(agent, base_image_b64, all_overlays)
    if composite_b64 == bu.SENTINEL:
        composite_b64 = base_image_b64

    proposal_text: str = ", ".join(f"{p.player}={p.notation}" for p in proposals)
    user_text: str = f"Proposed moves: {proposal_text}. Pick the best one."

    vlm_request: dict[str, Any] = bu.make_vlm_request_with_image(
        ARBITER_SYSTEM, composite_b64, user_text,
    )
    bu.ui_status(agent, "deciding")
    with image_sem:
        reply: str = bu.vlm_text(agent, vlm_request)

    # bu.ui_vlm_cycle(
    #     agent,
    #     system_prompt=ARBITER_SYSTEM,
    #     user_message=user_text,
    #     raw_image_b64=base_image_b64,
    #     annotated_image_b64=composite_b64,
    #     vlm_reply=reply,
    #     overlays=all_overlays,
    # )

    print(f"  arbiter chose: {reply!r}")

    chosen: tuple[int, int, int, int] | None = _parse_squares(reply)
    if chosen is None:
        for p in proposals:
            if p.notation.lower() in reply.lower():
                return p
        bu.ui_status(agent, "could not decide")
        return proposals[0] if proposals else None

    for p in proposals:
        if p.from_col == chosen[0] and p.from_row == chosen[1] and p.to_col == chosen[2] and p.to_row == chosen[3]:
            return p

    return Proposal(
        player=agent, color=AGENT_COLORS[agent],
        from_col=chosen[0], from_row=chosen[1],
        to_col=chosen[2], to_row=chosen[3],
        notation=f"{_col_row_to_notation(chosen[0], chosen[1])}{_col_row_to_notation(chosen[2], chosen[3])}",
    )


def _execute_move(
    cfg: ChessConfig,
    move: Proposal,
) -> str:
    agent: str = "arbiter"
    fx, fy = bu.grid_to_norm(move.from_col, move.from_row, cfg.grid_size)
    tx, ty = bu.grid_to_norm(move.to_col, move.to_row, cfg.grid_size)

    bu.ui_status(agent, f"executing {move.notation}")
    print(f"  arbiter: executing drag {move.notation}")

    bu.device(agent, cfg.region, [{"type": "drag", "x1": fx, "y1": fy, "x2": tx, "y2": ty}])
    time.sleep(1.0)

    new_b64: str = bu.capture(agent, cfg.region, scale=cfg.scale)
    if new_b64 == bu.SENTINEL:
        bu.ui_error(agent, "post-move capture failed")
        return bu.SENTINEL

    arrow: list[dict[str, Any]] = bu.make_arrow_overlay(
        move.from_col, move.from_row, move.to_col, move.to_row,
        "#ffffff", cfg.grid_size, stroke_width=10, label=f"PLAYED: {move.notation}",
    )
    annotated: str = bu.annotate(agent, new_b64, arrow)
    # if annotated == bu.SENTINEL:
    #     annotated = new_b64
    #
    # bu.ui_vlm_cycle(
    #     agent,
    #     system_prompt="",
    #     user_message=f"Executed move: {move.notation}",
    #     raw_image_b64=new_b64,
    #     annotated_image_b64=annotated,
    #     vlm_reply=f"Move {move.notation} executed by {move.player}",
    #     overlays=arrow,
    # )

    bu.ui_status(agent, f"played {move.notation}")
    return new_b64


def _run_round(
    cfg: ChessConfig,
    grid_overlays: list[dict[str, Any]],
    image_sem: threading.Semaphore,
    text_sem: threading.Semaphore,
    prev_image_b64: str,
) -> str:
    if prev_image_b64 == bu.SENTINEL:
        base_b64: str = bu.capture("arbiter", cfg.region, scale=cfg.scale)
        if base_b64 == bu.SENTINEL:
            bu.ui_error("arbiter", "initial capture failed")
            return bu.SENTINEL
    else:
        base_b64 = prev_image_b64

    results: dict[str, Proposal | None] = {}
    lock: threading.Lock = threading.Lock()

    def _player_thread(name: str, color: str, prompt: str) -> None:
        result: Proposal | None = _player_cycle(
            name, color, prompt, cfg, base_b64, grid_overlays,
            image_sem, text_sem,
        )
        with lock:
            results[name] = result

    threads: list[threading.Thread] = [
        threading.Thread(target=_player_thread, args=(n, c, p))
        for n, c, p in PLAYERS
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    proposals: list[Proposal] = [p for p in results.values() if p is not None]
    print(f"  proposals: {[p.notation for p in proposals]}")

    if not proposals:
        bu.ui_status("arbiter", "no proposals received")
        return bu.SENTINEL

    chosen: Proposal | None = _arbiter_decide(
        cfg, proposals, base_b64, grid_overlays, image_sem,
    )
    if chosen is None:
        bu.ui_status("arbiter", "arbiter could not decide")
        return bu.SENTINEL

    return _execute_move(cfg, chosen)


def _wait_for_panel(cfg: ChessConfig) -> None:
    url: str = "http://127.0.0.1:1236/ready"
    for attempt in range(cfg.ready_poll_max):
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    data: dict[str, Any] = json.loads(resp.read())
                    if data.get("ui_connected"):
                        print(f"Panel UI connected (attempt {attempt + 1})")
                        return
        except Exception:
            pass
        time.sleep(cfg.ready_poll_interval)
    print("WARNING: panel UI not connected after polling, proceeding anyway")


def main() -> None:
    args: bu.BrainArgs = bu.parse_brain_args(sys.argv[1:])
    cfg: ChessConfig = ChessConfig(region=args.region, scale=args.scale)
    print(f"chess_players started region={cfg.region} scale={cfg.scale}")

    grid_overlays: list[dict[str, Any]] = bu.make_grid_overlays(
        cfg.grid_size, cfg.grid_color, cfg.grid_stroke_width,
    )

    image_sem: threading.Semaphore = threading.Semaphore(cfg.vlm_image_semaphore_count)
    text_sem: threading.Semaphore = threading.Semaphore(cfg.vlm_text_semaphore_count)

    _wait_for_panel(cfg)
    time.sleep(cfg.post_ready_delay)

    prev_board: str = bu.SENTINEL
    round_gate: threading.Event = threading.Event()
    round_gate.set()

    def fire_next(board_b64: str) -> None:
        nonlocal prev_board
        prev_board = board_b64
        bu.push("arbiter", ["arbiter"], event_type="next_round", board_b64=board_b64)

    def on_sse(event: str, data: dict[str, Any]) -> None:
        nonlocal prev_board
        if event == "connected":
            if round_gate.is_set():
                round_gate.clear()
                print("=== SSE connected, firing first round ===")

                def _first_round() -> None:
                    result: str = bu.SENTINEL
                    try:
                        print("=== new round ===")
                        result = _run_round(cfg, grid_overlays, image_sem, text_sem, bu.SENTINEL)
                    finally:
                        round_gate.set()
                    fire_next(result)

                threading.Thread(target=_first_round, daemon=True).start()
            return
        if event != "message":
            return
        if data.get("event_type") != "next_round":
            return
        if not round_gate.is_set():
            return
        round_gate.clear()
        board: str = data.get("board_b64", bu.SENTINEL)
        prev_board = board

        def _round_worker() -> None:
            result: str = bu.SENTINEL
            try:
                print("=== new round ===")
                result = _run_round(cfg, grid_overlays, image_sem, text_sem, prev_board)
            finally:
                round_gate.set()
            fire_next(result)

        threading.Thread(target=_round_worker, daemon=True).start()

    sse_url: str = f"{bu.SSE_BASE_URL}?agent=arbiter"
    bu.sse_listen(sse_url, on_sse)

    threading.Event().wait()


if __name__ == "__main__":
    main()

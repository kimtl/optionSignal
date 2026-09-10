from __future__ import annotations

import argparse
import json
import sys
from typing import Sequence

from .collector import DEFAULT_INTERVAL
from .fetch import DEFAULT_SYMBOL, fetch_chain
from .settings import default_interval, default_otm_points, default_symbol, env_int, env_str, listen_port
from .signal import DEFAULT_BAND, DEFAULT_HEADLINE_DTE, build_report
from .store import load_history, save_snapshot


def _fmt_pct(value: float | None, digits: int = 1) -> str:
    if value is None:
        return "—"
    return f"{value * 100:.{digits}f}%"


def _fmt_num(value: float | None, digits: int = 2) -> str:
    if value is None:
        return "—"
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    return f"{value:.{digits}f}"


def _print_report(report) -> None:
    cppi = report.headline_cppi
    print()
    print(f"  {report.symbol}  spot {_fmt_num(report.spot, 2)}   "
          f"{report.futures_symbol} {_fmt_num(report.futures_price, 2)}")
    print(f"  asof {report.asof}   source {report.source}   DTE≤{report.max_dte}")
    print()
    print(f"  CPPI (콜-풋 프리미엄 불균형)   {_fmt_num(cppi, 3) if cppi is not None else '—'}")
    print(f"  콜 거래대금 가중 프리미엄      {_fmt_num(report.headline_call_premium)}   "
          f"vol {report.headline_call_volume:,}")
    print(f"  풋 거래대금 가중 프리미엄      {_fmt_num(report.headline_put_premium)}   "
          f"vol {report.headline_put_volume:,}")
    print(f"  콜/풋 프리미엄 비율            {_fmt_num(report.premium_ratio, 3)}")
    print(f"  리스크 리버설 (25Δ 콜IV-풋IV)  {_fmt_pct(report.risk_reversal)}")
    print(f"  OTM 콜 IV / 풋 IV              {_fmt_pct(report.otm_call_iv)} / {_fmt_pct(report.otm_put_iv)}")
    print(f"  세션 급등 갭 (콜%−풋%)         {_fmt_num(report.session_surge_gap, 1)}")
    print(f"  bias {report.bias}   score {report.score:+d}")
    print()
    print(f"  {report.summary_ko}")
    print()
    print("  expiry      dte   cppi   call$    put$   callIV  putIV     RR  surgeΔ")
    print("  " + "-" * 72)
    for sl in report.slices:
        surge_gap = None
        if sl.call_surge_pct is not None and sl.put_surge_pct is not None:
            surge_gap = sl.call_surge_pct - sl.put_surge_pct
        print(
            f"  {sl.expiry}  {sl.dte:>3}  {_fmt_num(sl.cppi, 3):>6}  "
            f"{_fmt_num(sl.call_premium):>7} {_fmt_num(sl.put_premium):>7}  "
            f"{_fmt_pct(sl.otm_call_iv):>6} {_fmt_pct(sl.otm_put_iv):>6}  "
            f"{_fmt_pct(sl.risk_reversal):>6}  {_fmt_num(surge_gap, 1)}"
        )
    print()


def _cmd_snapshot(args: argparse.Namespace) -> int:
    chain = fetch_chain(symbol=args.symbol, max_dte=args.max_dte, expiry_limit=max(1, args.max_dte + 1))
    report = build_report(
        chain, max_dte=args.max_dte, moneyness_band=args.band, otm_points=args.otm_points
    )
    if not args.no_save:
        save_snapshot(report)
    if args.json:
        json.dump(report.to_dict(), sys.stdout, ensure_ascii=False, indent=2)
        print()
    else:
        _print_report(report)
        if not args.no_save:
            print("  snapshot saved to data/optionsignal.db")
    return 0


def _cmd_history(args: argparse.Namespace) -> int:
    rows = load_history(args.symbol, limit=args.limit)
    if args.json:
        json.dump(rows, sys.stdout, ensure_ascii=False, indent=2)
        print()
        return 0
    if not rows:
        print("no ticks yet — run: python -m optionsignal")
        return 0
    print(f"{'ts':<22} {'cppi':>7} {'ratio':>7} {'RR':>8} {'bias':<10} score")
    for item in rows:
        print(
            f"{item.get('asof', item.get('stored_at', '')):<22} "
            f"{_fmt_num(item.get('headline_cppi'), 3):>7} "
            f"{_fmt_num(item.get('premium_ratio'), 3):>7} "
            f"{_fmt_pct(item.get('risk_reversal')):>8} "
            f"{item.get('bias', ''):<10} {item.get('score', 0):+d}"
        )
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .web import create_app

    app = create_app(
        symbol=args.symbol,
        max_dte=args.max_dte,
        interval=args.interval,
        start_collector=True,
        otm_points=getattr(args, "otm_points", None),
    )
    import logging

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    app.state.hubs.port = args.port
    for hub in app.state.hubs.hubs.values():
        hub.port = args.port
    print(f"optionSignal  http://{args.host}:{args.port}")
    if args.host == "0.0.0.0":
        print("Railway면 대시보드에서 Generate Domain 한 뒤 그 URL을 같이 보면 됩니다.")
    else:
        print("같은 네트워크의 다른 사람은 이 컴퓨터의 IP:포트로 같이 보면 됩니다.")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="optionsignal",
        description="Minute-level Nasdaq call vs put premium board (shared web UI).",
    )
    sub = parser.add_subparsers(dest="command")

    snap = sub.add_parser("snapshot", help="One-shot fetch (debug)")
    snap.add_argument("--symbol", default=DEFAULT_SYMBOL, help="QQQ (default) or ^NDX")
    snap.add_argument("--max-dte", type=int, default=DEFAULT_HEADLINE_DTE)
    snap.add_argument("--band", type=float, default=DEFAULT_BAND)
    snap.add_argument(
        "--otm-points", type=float, default=default_otm_points(),
        help="Strike distance from ATM in index points (e.g. 100/150/200). Omit for the percent band.",
    )
    snap.add_argument("--json", action="store_true")
    snap.add_argument("--no-save", action="store_true")
    snap.set_defaults(func=_cmd_snapshot)

    hist = sub.add_parser("history", help="Show saved minute ticks")
    hist.add_argument("--symbol", default=DEFAULT_SYMBOL)
    hist.add_argument("--limit", type=int, default=50)
    hist.add_argument("--json", action="store_true")
    hist.set_defaults(func=_cmd_history)

    serve = sub.add_parser("serve", help="Shared web board (default)")
    serve.add_argument("--symbol", default=default_symbol())
    serve.add_argument("--max-dte", type=int, default=env_int("OPTIONSIGNAL_MAX_DTE", DEFAULT_HEADLINE_DTE))
    serve.add_argument("--interval", type=int, default=default_interval(), help="Seconds between board snapshots")
    serve.add_argument(
        "--otm-points", type=float, default=default_otm_points(),
        help="CPPI strike distance from ATM in index points (100/150/200). Omit for the percent band.",
    )
    serve.add_argument("--host", default="0.0.0.0", help="Bind address so others can join")
    serve.add_argument("--port", type=int, default=listen_port(), help="Port. Railway sets PORT automatically.")
    serve.set_defaults(func=_cmd_serve)

    dash = sub.add_parser("dashboard", help="Alias for serve")
    dash.add_argument("--symbol", default=DEFAULT_SYMBOL)
    dash.add_argument("--max-dte", type=int, default=DEFAULT_HEADLINE_DTE)
    dash.add_argument("--interval", type=int, default=default_interval())
    dash.add_argument("--host", default="0.0.0.0")
    dash.add_argument("--port", type=int, default=listen_port())
    dash.set_defaults(func=_cmd_serve)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv:
        argv = ["serve"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "func", None):
        parser.print_help()
        return 2
    return args.func(args)

#!/usr/bin/env python3
"""
manage.py - admin CLI for the OpenAI relay (users + usage reports).

Operates directly on relay.db. Examples:
  ./manage.py adduser alice --daily-token-limit 2000000 --rpm 30
  ./manage.py listusers
  ./manage.py disable alice
  ./manage.py enable alice
  ./manage.py setlimit alice --daily-token-limit 5000000
  ./manage.py setexpiry alice --days 30
  ./manage.py deluser alice
  ./manage.py report                 # all users, all time
  ./manage.py report --user alice --days 7
"""

import sys
import time
import argparse

from openai_relay import db as relay_db


def _epoch_ms_in_days(days):
    return int(time.time() * 1000) + int(days * 86400 * 1000)


def cmd_adduser(a):
    exp = _epoch_ms_in_days(a.days) if a.days else None
    key = relay_db.add_user(
        a.username,
        daily_token_limit=a.daily_token_limit,
        daily_request_limit=a.daily_request_limit,
        rpm_limit=a.rpm,
        expires_at=exp,
        note=a.note,
    )
    print(f"用户已创建: {a.username}")
    print(f"API key   : {key}")
    print("把这个 key 配到该用户的 cc switch 接口里。")


def cmd_listusers(a):
    users = relay_db.list_users()
    if not users:
        print("(无用户)")
        return
    print(f"{'用户':<14}{'状态':<6}{'日token上限':>14}{'日请求上限':>12}{'rpm':>6}  过期        key")
    for u in users:
        exp = u.get("expires_at")
        exp_s = time.strftime("%Y-%m-%d", time.localtime(exp/1000)) if exp else "永久"
        print(f"{u['username']:<14}{'启用' if u['enabled'] else '禁用':<6}"
              f"{(u['daily_token_limit'] or '-'):>14}"
              f"{(u['daily_request_limit'] or '-'):>12}"
              f"{(u['rpm_limit'] or '-'):>6}  {exp_s:<11} {u['api_key']}")


def cmd_disable(a):
    n = relay_db.set_enabled(username=a.username, enabled=0)
    print(f"已禁用 {a.username}" if n else "未找到该用户")


def cmd_enable(a):
    n = relay_db.set_enabled(username=a.username, enabled=1)
    print(f"已启用 {a.username}" if n else "未找到该用户")


def cmd_setlimit(a):
    # 只改"提供了 flag"的字段;值为 0 表示清成无限(NULL)。
    kw = {}
    for flag, col in (("daily_token_limit", "daily_token_limit"),
                      ("daily_request_limit", "daily_request_limit"),
                      ("rpm", "rpm_limit")):
        v = getattr(a, flag)
        if v is not None:                      # 该 flag 被指定了
            kw[col] = None if v == 0 else v    # 0 -> 无限
    if not kw:
        print("未指定任何限额(用 0 表示不限,如 --daily-token-limit 0)")
        return
    n = relay_db.update_limits(username=a.username, **kw)
    print(f"已更新 {a.username}" if n else "未更新(检查用户名)")


def cmd_setexpiry(a):
    exp = _epoch_ms_in_days(a.days)
    n = relay_db.update_limits(username=a.username, expires_at=exp)
    print(f"已设置 {a.username} {a.days} 天后过期" if n else "未找到该用户")


def cmd_deluser(a):
    n = relay_db.delete_user(username=a.username)
    print(f"已删除 {a.username}" if n else "未找到该用户")


def cmd_reprice(a):
    result = relay_db.reprice_usage(apply=a.apply)
    action = "已应用" if a.apply else "预览"
    print(f"{action}: 可重算 {result['eligible']} 条，需更新 {result['changed']} 条")
    print(f"费用总计: ${result['before']:.6f} -> ${result['after']:.6f}")
    if not a.apply:
        print("未修改数据库；停止 openai-relay 并确认备份后，使用 reprice --apply 执行。")


def cmd_report(a):
    conn = relay_db.connect(readonly=True)
    where, params = [], []
    if a.user:
        where.append("username=?"); params.append(a.user)
    if a.days:
        since = int((time.time() - a.days * 86400) * 1000)
        where.append("ts>=?"); params.append(since)
    wsql = ("WHERE " + " AND ".join(where)) if where else ""

    print("=== 按用户 ===")
    rows = conn.execute(
        f"SELECT username, COUNT(*) reqs, SUM(input_tokens) it, SUM(output_tokens) ot,"
        f" SUM(cache_creation_tokens) ct, SUM(cache_read_tokens) rt, SUM(cost_usd) cost"
        f" FROM usage {wsql} GROUP BY username ORDER BY cost DESC", params).fetchall()
    print(f"{'用户':<14}{'请求':>8}{'输入':>12}{'输出':>12}{'缓存写':>12}{'缓存读':>12}{'费用$':>10}")
    for r in rows:
        print(f"{r['username']:<14}{r['reqs']:>8}{(r['it'] or 0):>12,}{(r['ot'] or 0):>12,}"
              f"{(r['ct'] or 0):>12,}{(r['rt'] or 0):>12,}{(r['cost'] or 0):>10.4f}")

    print("\n=== 按天(近30) ===")
    rows = conn.execute(
        f"SELECT day, COUNT(*) reqs, SUM(input_tokens+output_tokens) tok, SUM(cost_usd) cost"
        f" FROM usage {wsql} GROUP BY day ORDER BY day DESC LIMIT 30", params).fetchall()
    for r in rows:
        print(f"{r['day']}  请求{r['reqs']:>6}  tokens{(r['tok'] or 0):>12,}  ${ (r['cost'] or 0):.4f}")

    if not a.user:
        rows = conn.execute(
            f"SELECT day, username, SUM(input_tokens+output_tokens) tok, SUM(cost_usd) cost"
            f" FROM usage {wsql} GROUP BY day, username"
            f" ORDER BY day DESC, cost DESC", params).fetchall()
        tok_du, cost_du, users_du, days_du = {}, {}, {}, []
        for r in rows:
            d = r["day"]; u = r["username"]
            if d not in tok_du:
                tok_du[d] = {}; cost_du[d] = {}; days_du.append(d)
            tok_du[d][u] = tok_du[d].get(u, 0) + (r["tok"] or 0)
            cost_du[d][u] = cost_du[d].get(u, 0) + (r["cost"] or 0)
            users_du[u] = users_du.get(u, 0) + (r["cost"] or 0)
        ucols = sorted(users_du, key=lambda x: users_du[x], reverse=True)
        if ucols:
            def htok_early(v):
                v = v or 0
                if v >= 1_000_000: return f"{v/1_000_000:.1f}M"
                if v >= 1000: return f"{v/1000:.0f}k"
                return str(int(v))
            col_w = max(9, max(len(u) for u in ucols) + 1)
            print(f"\n=== 按天 x 用户(近30,Token) ===")
            print(f"{'日期':<12}" + "".join(f"{u:>{col_w}}" for u in ucols)
                  + f"{'合计':>11}{'费用$':>10}")
            for d in days_du[:30]:
                tt = sum(tok_du[d].get(u, 0) for u in ucols)
                tc = sum(cost_du[d].get(u, 0) for u in ucols)
                line = f"{d:<12}" + "".join(
                    f"{(htok_early(tok_du[d].get(u,0)) if tok_du[d].get(u,0) else chr(8212)):>{col_w}}"
                    for u in ucols)
                print(line + f"{htok_early(tt):>11}{tc:>10.2f}")

    print("\n=== 按模型 ===")
    rows = conn.execute(
        f"SELECT model, COUNT(*) reqs, SUM(input_tokens) it, SUM(output_tokens) ot, SUM(cost_usd) cost"
        f" FROM usage {wsql} GROUP BY model ORDER BY cost DESC", params).fetchall()
    for r in rows:
        print(f"{(r['model'] or '-'):<26}请求{r['reqs']:>6}  in{(r['it'] or 0):>11,}  "
              f"out{(r['ot'] or 0):>11,}  ${ (r['cost'] or 0):.4f}")

    def htok(v):
        v = v or 0
        if v >= 1_000_000:
            return f"{v/1_000_000:.1f}M"
        if v >= 1000:
            return f"{v/1000:.0f}k"
        return str(int(v))

    def pivot(title, rows, bucket_of, col_order, col_label):
        """One row per user; buckets become columns (io-tokens), plus 合计/费用.
        Mirrors the /stats cross-tab so the CLI report isn't a scattered list."""
        cells, tt, tc = {}, {}, {}
        for r in rows:
            u = r["username"]; b = bucket_of(r)
            cells.setdefault(u, {})
            cells[u][b] = cells[u].get(b, 0) + (r["tok"] or 0)
            tt[u] = tt.get(u, 0) + (r["tok"] or 0)
            tc[u] = tc.get(u, 0) + (r["cost"] or 0)
        cols = [c for c in col_order if any(cells[u].get(c, 0) for u in cells)]
        print(f"\n=== {title} ===")
        print(f"{'用户':<14}" + "".join(f"{col_label(c):>9}" for c in cols)
              + f"{'合计':>11}{'费用$':>10}")
        for u in sorted(cells, key=lambda x: tc.get(x, 0), reverse=True):
            line = f"{u:<14}" + "".join(
                f"{(htok(cells[u].get(c,0)) if cells[u].get(c,0) else '—'):>9}" for c in cols)
            print(line + f"{htok(tt.get(u,0)):>11}{(tc.get(u,0)):>10.2f}")

    rows = conn.execute(
        f"SELECT username, model, SUM(input_tokens+output_tokens) tok, SUM(cost_usd) cost"
        f" FROM usage {wsql} GROUP BY username, model", params).fetchall()
    pivot("按用户 × 模型(Token)", rows,
          lambda r: relay_db.model_family(r["model"]),
          relay_db.MODEL_FAMILIES, lambda c: c)

    cli_lbl = {"cli": "CLI", "desktop": "桌面版", "other": "其他"}
    rows = conn.execute(
        f"SELECT username, COALESCE(client,'other') client,"
        f" SUM(input_tokens+output_tokens) tok, SUM(cost_usd) cost"
        f" FROM usage {wsql} GROUP BY username, COALESCE(client,'other')", params).fetchall()
    pivot("按用户 × 客户端(Token)", rows,
          lambda r: r["client"] if r["client"] in ("cli", "desktop") else "other",
          ["cli", "desktop", "other"], lambda c: cli_lbl.get(c, c))
    conn.close()


def main():
    relay_db.init_db()
    p = argparse.ArgumentParser(description="OpenAI relay 管理")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("adduser"); s.add_argument("username")
    s.add_argument("--daily-token-limit", type=int)
    s.add_argument("--daily-request-limit", type=int)
    s.add_argument("--rpm", type=int)
    s.add_argument("--days", type=float, help="N 天后过期")
    s.add_argument("--note")
    s.set_defaults(func=cmd_adduser)

    s = sub.add_parser("listusers"); s.set_defaults(func=cmd_listusers)

    s = sub.add_parser("disable"); s.add_argument("username"); s.set_defaults(func=cmd_disable)
    s = sub.add_parser("enable"); s.add_argument("username"); s.set_defaults(func=cmd_enable)

    s = sub.add_parser("setlimit", help="改限额;值填 0 表示清成无限")
    s.add_argument("username")
    s.add_argument("--daily-token-limit", type=int, help="每日 token 上限;0=不限")
    s.add_argument("--daily-request-limit", type=int, help="每日请求上限;0=不限")
    s.add_argument("--rpm", type=int, help="每分钟请求上限;0=不限")
    s.set_defaults(func=cmd_setlimit)

    s = sub.add_parser("setexpiry"); s.add_argument("username")
    s.add_argument("--days", type=float, required=True)
    s.set_defaults(func=cmd_setexpiry)

    s = sub.add_parser("deluser"); s.add_argument("username"); s.set_defaults(func=cmd_deluser)

    s = sub.add_parser("reprice", help="重算已记录的已知模型费用;默认仅预览")
    s.add_argument("--apply", action="store_true", help="写入重算后的费用")
    s.set_defaults(func=cmd_reprice)

    s = sub.add_parser("report")
    s.add_argument("--user"); s.add_argument("--days", type=float)
    s.set_defaults(func=cmd_report)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

"""Dados e geometria dos gráficos do painel (SVG gerado no servidor, sem bibliotecas JS)."""

from datetime import timedelta
from math import ceil, log10

from django.db.models import Count
from django.utils import timezone

from invoices.models import STATUS_GROUPS

WIDTH, HEIGHT = 680, 230
LEFT, RIGHT, TOP, BOTTOM = 36, 8, 12, 26

# Ordem fixa dos segmentos (validada para daltonismo: verde, azul, laranja, vermelho).
STATUS_ORDER = [("enviadas", "Enviadas", "sent"), ("pendentes", "Pendentes", "pending"),
                ("rejeitadas", "Rejeitadas", "rejected"), ("erros", "Erros", "error")]


def _nice_max(value: int) -> int:
    if value <= 4:
        return 4
    magnitude = 10 ** int(log10(value))
    for step in (1, 2, 2.5, 5, 10):
        top = step * magnitude
        if top >= value:
            return int(ceil(top))
    return value


def _bar_path(x: float, y: float, w: float, h: float, base: float) -> str:
    """Barra com os cantos de cima arredondados (4px) e ancorada na linha de base."""
    r = min(4.0, h, w / 2)
    f = lambda v: f"{v:.1f}"  # noqa: E731
    return (f"M{f(x)},{f(base)} V{f(y + r)} Q{f(x)},{f(y)} {f(x + r)},{f(y)} H{f(x + w - r)} "
            f"Q{f(x + w)},{f(y)} {f(x + w)},{f(y + r)} V{f(base)} Z")


def daily_documents(invoices, days: int = 14) -> dict:
    """Documentos por data do documento, nos últimos `days` dias (inclui hoje)."""
    today = timezone.localdate()
    start = today - timedelta(days=days - 1)
    raw = dict(invoices.filter(document_date__gte=start, document_date__lte=today)
               .values_list("document_date").annotate(n=Count("id")))
    top = _nice_max(max(raw.values(), default=0))
    plot_w, plot_h = WIDTH - LEFT - RIGHT, HEIGHT - TOP - BOTTOM
    slot = plot_w / days
    bar_w = max(6, min(28, slot * 0.56))
    bars = []
    for i in range(days):
        day = start + timedelta(days=i)
        count = raw.get(day, 0)
        h = round(plot_h * count / top, 1) if count else 0
        x = LEFT + slot * i + (slot - bar_w) / 2
        base = TOP + plot_h
        bars.append({
            "d": _bar_path(x, base - h, bar_w, h, base) if h else "",
            "date": day, "count": count, "x": round(x, 1), "w": round(bar_w, 1), "h": h,
            "y": round(TOP + plot_h - h, 1), "cx": round(x + bar_w / 2, 1),
            "slot_x": round(LEFT + slot * i, 1), "slot_w": round(slot, 1),
            "label": day.strftime("%d/%m"), "show_label": i % 2 == (days - 1) % 2,
            "tip_y": round(max(TOP, TOP + plot_h - h - 30), 1),
        })
    fractions = (0, .25, .5, .75, 1) if top % 4 == 0 else (0, .5, 1) if top % 2 == 0 else (0, 1)
    ticks = [{"value": int(top * f), "y": round(TOP + plot_h - plot_h * f, 1)} for f in fractions]
    return {"bars": bars, "ticks": ticks, "width": WIDTH, "height": HEIGHT, "left": LEFT,
            "right": WIDTH - RIGHT, "baseline": TOP + plot_h, "label_y": HEIGHT - 6,
            "total": sum(raw.values()), "days": days}


def status_distribution(counts_by_status: dict) -> dict:
    groups = {key: sum(counts_by_status.get(s, 0) for s in statuses) for key, statuses in STATUS_GROUPS.items()}
    total = sum(groups.values())
    segments = [
        {"key": key, "label": label, "css": css, "count": groups[key],
         "pct": round(100 * groups[key] / total, 1) if total else 0}
        for key, label, css in STATUS_ORDER
    ]
    return {"segments": segments, "total": total}

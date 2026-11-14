"""Draw benchmarks/context_efficiency.svg from benchmarks/results.json.

The chart has two lines on log-log axes: the bytes of the rows as JSON, and the protocol bytes
that go to the caller. It uses only the standard library, so it needs no plotting package.

Run:  python benchmarks/plot.py
"""
import json
import math
from pathlib import Path

HERE = Path(__file__).parent
W, H = 720, 380
LEFT, RIGHT, TOP, BOTTOM = 92, 196, 56, 64
Y_MIN, Y_MAX = 3, 11  # log10 of bytes: 1 kB to 100 GB
X_MIN, X_MAX = 4, 8  # log10 of rows

SERIES = [  # (key, label, css class)
    ("export_bytes", "Rows as JSON", "s2"),
    ("protocol_bytes", "Protocol bytes to the caller", "s1"),
]


def x(rows: float) -> float:
    return LEFT + (math.log10(rows) - X_MIN) / (X_MAX - X_MIN) * (W - LEFT - RIGHT)


def y(size: float) -> float:
    return TOP + (Y_MAX - math.log10(size)) / (Y_MAX - Y_MIN) * (H - TOP - BOTTOM)


def size_label(exp: int) -> str:
    return ["1 B", "10 B", "100 B", "1 kB", "10 kB", "100 kB", "1 MB", "10 MB", "100 MB", "1 GB", "10 GB", "100 GB", "1 TB", "10 TB"][exp]


def bytes_text(n: int) -> str:
    for unit, scale in (("GB", 1e9), ("MB", 1e6), ("kB", 1e3)):
        if n >= scale:
            return f"{n / scale:.1f} {unit}"
    return f"{n} B"


def main() -> None:
    runs = json.loads((HERE / "results.json").read_text())
    out = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="-apple-system, BlinkMacSystemFont, Segoe UI, Helvetica, Arial, sans-serif" role="img" aria-labelledby="t d">',
           "<title id=\"t\">Bytes to the caller versus dataset size</title>",
           "<desc id=\"d\">" + "; ".join(f"{r['rows']:,} rows: rows as JSON {r['export_bytes']:,} B, protocol bytes {r['protocol_bytes']:,} B" for r in runs) + "</desc>",
           """<style>
  .bg { fill: #fcfcfb } .t1 { fill: #0b0b0b } .t2 { fill: #52514e } .grid { stroke: #e4e3df } .axis { stroke: #8f8e88 }
  .s1 { stroke: #2a78d6 } .s1f { fill: #2a78d6 } .s2 { stroke: #eb6834 } .s2f { fill: #eb6834 } .ring { stroke: #fcfcfb }
  @media (prefers-color-scheme: dark) {
    .bg { fill: #1a1a19 } .t1 { fill: #f0efec } .t2 { fill: #c3c2b7 } .grid { stroke: #333331 } .axis { stroke: #6d6c66 }
    .s1 { stroke: #3987e5 } .s1f { fill: #3987e5 } .s2 { stroke: #d95926 } .s2f { fill: #d95926 } .ring { stroke: #1a1a19 }
  }
</style>""",
           f'<rect class="bg" width="{W}" height="{H}" rx="6"/>',
           f'<text class="t1" x="{LEFT}" y="26" font-size="15" font-weight="600">One question, five operations: bytes that reach the caller</text>',
           f'<text class="t2" x="{LEFT}" y="44" font-size="12">Log scales. The dataset grows 10,000 times. The protocol bytes grow by 9 bytes.</text>']
    for e in range(Y_MIN, Y_MAX + 1, 2):
        out.append(f'<line class="grid" x1="{LEFT}" x2="{W - RIGHT}" y1="{y(10 ** e):.1f}" y2="{y(10 ** e):.1f}" stroke-width="1"/>')
        out.append(f'<text class="t2" x="{LEFT - 10}" y="{y(10 ** e) + 4:.1f}" font-size="11" text-anchor="end">{size_label(e)}</text>')
    for e in range(X_MIN, X_MAX + 1):
        out.append(f'<text class="t2" x="{x(10 ** e):.1f}" y="{H - BOTTOM + 20}" font-size="11" text-anchor="middle">{10 ** e:,}</text>')
    out.append(f'<line class="axis" x1="{LEFT}" x2="{W - RIGHT}" y1="{H - BOTTOM}" y2="{H - BOTTOM}" stroke-width="1"/>')
    out.append(f'<text class="t2" x="{(LEFT + W - RIGHT) / 2:.1f}" y="{H - 18}" font-size="12" text-anchor="middle">Rows in the events table</text>')
    for key, label, css in SERIES:
        pts = [(x(r["rows"]), y(r[key])) for r in runs]
        out.append(f'<polyline class="{css}" fill="none" stroke-width="2" stroke-linejoin="round" points="{" ".join(f"{a:.1f},{b:.1f}" for a, b in pts)}"/>')
        for (a, b), r in zip(pts, runs):
            out.append(f'<circle class="{css}f ring" cx="{a:.1f}" cy="{b:.1f}" r="4.5" stroke-width="2"><title>{r["rows"]:,} rows: {r[key]:,} B</title></circle>')
        last = runs[-1]
        lx, ly = pts[-1]
        out.append(f'<text class="t1" x="{lx + 12:.1f}" y="{ly - 2:.1f}" font-size="12" font-weight="600">{label}</text>')
        out.append(f'<text class="t2" x="{lx + 12:.1f}" y="{ly + 13:.1f}" font-size="11">{bytes_text(last[key])} at {last["rows"]:,}</text>')
    out.append("</svg>")
    (HERE / "context_efficiency.svg").write_text("\n".join(out) + "\n")


if __name__ == "__main__":
    main()

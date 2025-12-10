#!/usr/bin/env python3
"""
Generate Google Play Store listing assets from the homepage showcase data.

The homepage already declares all screenshot sources, captions, and descriptions
for phone and tablet layouts. This tool parses that template, loads the same
LitElement-powered frames, and renders framed screenshots via Playwright so
that every asset can be regenerated from a single source of truth.

Example usage (phone screenshots only for now):
    python tools/generate_play_store_assets.py --device phone
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import re
import sys
from dataclasses import dataclass
from html import escape
from pathlib import Path
from textwrap import dedent
from typing import Dict, Iterable, List, Sequence

try:
    from playwright.async_api import Browser, Page, async_playwright
except ImportError:  # pragma: no cover - surfaced as runtime error for CLI users
    async_playwright = None


TEMPLATE_PATH = Path("server/portacode_django/templates/pages/home.html")
STATIC_ROOT = Path("server/portacode_django/static")

# Map component tags to their module files so we can inline the LitElement code.
COMPONENT_MODULES = {
    "pc-screenshot-frame": Path("server/portacode_django/static/js/components/screenshot-frame.js"),
    "pc-tablet-frame": Path("server/portacode_django/static/js/components/tablet-screenshot-frame.js"),
}

IMPORT_PATTERN = re.compile(r'^(\s*)import\s+(.+?)\s+from\s+[\'"](.+?)[\'"];?\s*$')

DEFAULT_FRAME_WIDTH = (0.78, 1400)  # ratio of viewport width, max pixels
DEVICE_FRAME_WIDTH = {
    "phone": (0.78, 1400),
    "tablet7": (0.82, 1800),
    "tablet10": (0.82, 2000),
}

DEFAULT_ORIENTATION = "portrait"
DEVICE_ORIENTATION = {
    "phone": "portrait",
    "tablet7": "landscape",
    "tablet10": "landscape",
}

DEFAULT_VIEWPORT = (1440, 2560)
DEVICE_VIEWPORT = {
    "phone": (1440, 2560),
    "tablet7": (2560, 1440),
    "tablet10": (2880, 1620),
}

FEATURE_GRAPHIC_SIZE = (1024, 500)
FEATURE_DEFAULT_CAPTION = "Operate labs and copilots from any screen."
FEATURE_DEFAULT_SELECTION = [
    ("phone", 1),
    ("tablet7", 2),
    ("tablet10", 4),
]


@dataclass
class ShowcaseItem:
    device: str
    src: str
    alt: str
    caption: str
    description: str


@dataclass
class DeviceMeta:
    label: str
    component: str


@dataclass
class FeatureCandidate:
    index: int
    device: str
    device_label: str
    device_ordinal: int
    filename: str
    caption: str
    description: str
    alt: str
    component: str
    asset_path: Path


@dataclass
class FeatureGraphicShot:
    device: str
    device_label: str
    caption: str
    alt: str
    component: str
    image_data: str


@dataclass
class FeatureGraphicInput:
    caption: str
    subline: str
    shots: List[FeatureCandidate]


class HomeShowcaseParser:
    """Extract showcaseData/deviceConfig objects directly from the homepage template."""

    SHOWCASE_RE = re.compile(r"const\s+showcaseData\s*=\s*\{(?P<body>.*?)\}\s*;", re.S)
    DEVICE_BLOCK_RE = re.compile(
        r"(?P<key>\w+)\s*:\s*\[(?P<body>.*?)\](?=,\s*\w+\s*:|\s*\}|\s*$)",
        re.S,
    )
    ITEM_RE = re.compile(
        r"""\{\s*src:\s*"(?P<src>.*?)",\s*alt:\s*'(?P<alt>.*?)',\s*caption:\s*'(?P<caption>.*?)',\s*description:\s*'(?P<description>.*?)'\s*\}""",
        re.S,
    )
    DEVICE_CONFIG_RE = re.compile(r"const\s+deviceConfig\s*=\s*\{(?P<body>.*?)\}\s*;", re.S)
    DEVICE_CONFIG_ITEM_RE = re.compile(
        r"(?P<key>\w+)\s*:\s*\{\s*label:\s*'(?P<label>.*?)'\s*,\s*component:\s*'(?P<component>.*?)'\s*\}",
        re.S,
    )
    STATIC_RE = re.compile(r"""\{\%\s*static\s+'([^']+)'\s*\%\}""")

    def __init__(self, template_path: Path) -> None:
        self.template_path = template_path
        self.content = template_path.read_text(encoding="utf-8")
        self.showcase_data = self._parse_showcase_data()
        self.device_meta = self._parse_device_config()

    def _parse_showcase_data(self) -> Dict[str, List[ShowcaseItem]]:
        match = self.SHOWCASE_RE.search(self.content)
        if not match:
            raise ValueError("Unable to locate showcaseData block in homepage template.")
        body = match.group("body")
        data: Dict[str, List[ShowcaseItem]] = {}
        for block in self.DEVICE_BLOCK_RE.finditer(body):
            key = block.group("key")
            items_text = block.group("body")
            items: List[ShowcaseItem] = []
            for item_match in self.ITEM_RE.finditer(items_text):
                items.append(
                    ShowcaseItem(
                        device=key,
                        src=item_match.group("src").strip(),
                        alt=item_match.group("alt").strip(),
                        caption=item_match.group("caption").strip(),
                        description=item_match.group("description").strip(),
                    )
                )
            if items:
                data[key] = items
        return data

    def _parse_device_config(self) -> Dict[str, DeviceMeta]:
        match = self.DEVICE_CONFIG_RE.search(self.content)
        if not match:
            raise ValueError("Unable to locate deviceConfig block in homepage template.")
        body = match.group("body")
        meta: Dict[str, DeviceMeta] = {}
        for item in self.DEVICE_CONFIG_ITEM_RE.finditer(body):
            key = item.group("key")
            label = self._unescape(item.group("label"))
            component = self._unescape(item.group("component"))
            meta[key] = DeviceMeta(label=label, component=component)
        return meta

    @staticmethod
    def _unescape(value: str) -> str:
        return value.replace(r"\'", "'").replace(r"\"", '"')

    def resolve_static_path(self, src_expr: str) -> Path:
        match = self.STATIC_RE.search(src_expr)
        if not match:
            raise ValueError(f"Unsupported src expression: {src_expr}")
        relative_path = match.group(1)
        path = STATIC_ROOT / relative_path
        if not path.exists():
            raise FileNotFoundError(f"Static asset not found: {path}")
        return path


def image_to_data_url(path: Path) -> str:
    mime = "image/png" if path.suffix.lower() == ".png" else "image/jpeg"
    payload = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime};base64,{payload}"


def slugify(value: str) -> str:
    value = value.lower()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-{2,}", "-", value).strip("-")
    return value or "screenshot"


def collect_feature_candidates(parser: HomeShowcaseParser) -> List[FeatureCandidate]:
    order = ["phone", "tablet7", "tablet10"]
    candidates: List[FeatureCandidate] = []
    index = 1
    for device in order:
        items = parser.showcase_data.get(device, [])
        if not items:
            continue
        meta = parser.device_meta.get(device)
        device_label = meta.label if meta else device.title()
        component = meta.component if meta else "pc-screenshot-frame"
        for ordinal, item in enumerate(items, start=1):
            asset_path = parser.resolve_static_path(item.src)
            candidates.append(
                FeatureCandidate(
                    index=index,
                    device=device,
                    device_label=device_label,
                    device_ordinal=ordinal,
                    filename=asset_path.name,
                    caption=item.caption,
                    description=item.description,
                    alt=item.alt,
                    component=component,
                    asset_path=asset_path,
                )
            )
            index += 1
    return candidates


def compute_default_feature_indices(candidates: Sequence[FeatureCandidate]) -> List[int]:
    resolved: List[int] = []
    for device, ordinal in FEATURE_DEFAULT_SELECTION:
        for candidate in candidates:
            if candidate.device == device and candidate.device_ordinal == ordinal:
                resolved.append(candidate.index)
                break
    for candidate in candidates:
        if len(resolved) >= 3:
            break
        if candidate.index not in resolved:
            resolved.append(candidate.index)
    return resolved[:3]


def display_feature_candidates(candidates: Sequence[FeatureCandidate]) -> None:
    if not candidates:
        print("No screenshots are available for feature graphic generation.")
        return
    print("\nAvailable screenshots for feature graphic:\n")
    for candidate in candidates:
        print(
            f"[{candidate.index:02d}] "
            f"{candidate.device_label:<12} "
            f"{candidate.filename:<40} "
            f"- {candidate.caption}"
        )
    print()


def parse_feature_pick_string(raw: str, total: int) -> List[int] | None:
    tokens = [token for token in re.split(r"[,\s]+", raw.strip()) if token]
    if not tokens:
        return None
    picks: List[int] = []
    for token in tokens:
        if not token.isdigit():
            return None
        value = int(token)
        if value < 1 or value > total:
            return None
        if value not in picks:
            picks.append(value)
    if len(picks) != 3:
        return None
    return picks


def determine_feature_indices(
    candidates: Sequence[FeatureCandidate],
    picks_arg: str | None,
    interactive: bool,
) -> List[int]:
    total = len(candidates)
    if total < 3:
        raise RuntimeError("Need at least three screenshots to build a feature graphic.")
    defaults = compute_default_feature_indices(candidates)
    if picks_arg:
        parsed = parse_feature_pick_string(picks_arg, total)
        if not parsed:
            raise ValueError(f"Invalid --feature-picks value '{picks_arg}'. Expected three indexes within 1-{total}.")
        return parsed
    if interactive:
        default_str = ", ".join(str(idx) for idx in defaults)
        raw = input(f"Select 3 screenshots by number [{default_str}]: ").strip()
        if raw:
            parsed = parse_feature_pick_string(raw, total)
            if parsed:
                return parsed
            print("Invalid selection, falling back to defaults.")
    return defaults


def resolve_feature_caption(args: argparse.Namespace, interactive: bool) -> str:
    default_caption = FEATURE_DEFAULT_CAPTION
    if getattr(args, "feature_caption", None):
        return args.feature_caption.strip()
    if interactive:
        raw = input(f"Feature caption [{default_caption}]: ").strip()
        if raw:
            return raw
    return default_caption


def prepare_feature_graphic_input(
    parser: HomeShowcaseParser,
    args: argparse.Namespace,
) -> FeatureGraphicInput:
    candidates = collect_feature_candidates(parser)
    if len(candidates) < 3:
        raise RuntimeError("At least three showcase screenshots are required to generate a feature graphic.")
    display_feature_candidates(candidates)
    interactive = sys.stdin.isatty()
    indices = determine_feature_indices(candidates, getattr(args, "feature_picks", None), interactive)
    order_map = {idx: position for position, idx in enumerate(indices)}
    selected = [candidate for candidate in candidates if candidate.index in order_map]
    selected.sort(key=lambda candidate: order_map[candidate.index])
    caption = resolve_feature_caption(args, interactive)
    device_labels = [shot.device_label for shot in selected]
    subline = " + ".join(device_labels) + " ready"
    print("Selected screenshots:")
    for shot in selected:
        print(f" - #{shot.index:02d} {shot.device_label} / {shot.filename} — {shot.caption}")
    print(f"Feature caption: {caption}\n")
    return FeatureGraphicInput(caption=caption, subline=subline, shots=selected)


def resolve_frame_width(device: str, viewport_width: int, override: str | None) -> str:
    if override:
        return override
    ratio, cap = DEVICE_FRAME_WIDTH.get(device, DEFAULT_FRAME_WIDTH)
    width = min(int(viewport_width * ratio), cap)
    return f"{width}px"


def build_html_document(
    item: ShowcaseItem,
    component_tag: str,
    component_source: str,
    frame_width_expr: str,
    orientation: str,
) -> str:
    """Compose a minimal HTML document that renders the framed screenshot."""
    if orientation == "landscape":
        styles = dedent(
            """
            :root {
                font-family: 'Space Grotesk', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                color: #f8fafc;
                --night: #01050f;
                --deep: #031126;
            }

            * {
                box-sizing: border-box;
            }

            body {
                margin: 0;
                width: 100%;
                height: 100%;
                min-height: 100vh;
                background:
                    radial-gradient(circle at 20% 35%, rgba(0,255,136,0.16), transparent 55%),
                    radial-gradient(circle at 85% 10%, rgba(56,189,248,0.25), transparent 50%),
                    linear-gradient(135deg, var(--night), var(--deep));
                display: flex;
                align-items: center;
                justify-content: center;
                overflow: hidden;
            }

            .frame-canvas {
                width: 100%;
                height: 100vh;
                overflow: hidden;
                padding: 70px 100px;
                display: flex;
                justify-content: space-between;
                align-items: center;
                gap: 80px;
                background:
                    linear-gradient(120deg, rgba(1,6,15,0.95), rgba(3,16,36,0.98)),
                    radial-gradient(circle at 35% 40%, rgba(0,0,0,0.28), transparent 75%);
            }

            .caption-block {
                flex: 0 0 34%;
                max-width: 620px;
            }

            h1 {
                font-size: 4.6rem;
                line-height: 1.05;
                margin: 0;
                color: #f8fafc;
            }

            .frame-area {
                flex: 1;
                display: flex;
                align-items: center;
                justify-content: center;
                height: calc(100% - 150px);
            }

            .frame-area pc-screenshot-frame,
            .frame-area pc-tablet-frame {
                width: FRAME_WIDTH_VALUE;
                filter:
                    drop-shadow(0 30px 110px rgba(0, 0, 0, 0.7))
                    drop-shadow(0 0 45px rgba(0, 255, 136, 0.14));
                transform: rotate(-1.1deg);
                transform-origin: center;
            }
            """
        )
    else:
        styles = dedent(
            """
            :root {
                font-family: 'Space Grotesk', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
                color: #f8fafc;
                --night: #01050f;
                --deep: #031126;
            }

            * {
                box-sizing: border-box;
            }

            body {
                margin: 0;
                width: 100%;
                height: 100%;
                min-height: 100vh;
                background:
                    radial-gradient(circle at 15% 10%, rgba(0,255,136,0.2), transparent 55%),
                    radial-gradient(circle at 80% 5%, rgba(56,189,248,0.25), transparent 50%),
                    linear-gradient(180deg, var(--night), var(--deep));
                display: flex;
                align-items: center;
                justify-content: center;
                overflow: hidden;
            }

            .frame-canvas {
                width: 100%;
                height: 100%;
                padding: 80px 80px 90px;
                display: flex;
                flex-direction: column;
                justify-content: flex-start;
                align-items: center;
                gap: 40px;
                background:
                    linear-gradient(135deg, rgba(2,8,20,0.92), rgba(3,16,36,0.98)),
                    radial-gradient(circle at center, rgba(0,0,0,0.25), transparent 80%);
            }

            .caption-block {
                width: 100%;
                max-width: 1100px;
                text-align: left;
                z-index: 2;
            }

            h1 {
                font-size: 5rem;
                line-height: 1.05;
                margin: 0;
                color: #f8fafc;
            }

            .frame-area {
                flex: 1;
                width: 100%;
                display: flex;
                align-items: center;
                justify-content: center;
                padding-top: 30px;
                margin-top: 30px;
                z-index: 1;
            }

            .frame-area pc-screenshot-frame,
            .frame-area pc-tablet-frame {
                width: FRAME_WIDTH_VALUE;
                filter:
                    drop-shadow(0 40px 120px rgba(0, 0, 0, 0.75))
                    drop-shadow(0 0 60px rgba(0, 255, 136, 0.1));
                transform: rotate(-2deg);
                transform-origin: center;
            }
            """
        )
    styles = styles.replace("FRAME_WIDTH_VALUE", frame_width_expr)

    html = f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>{item.caption}</title>
    <style>{styles}</style>
</head>
<body>
    <div class="frame-canvas">
        <header class="caption-block">
            <h1>{item.caption}</h1>
        </header>
        <div class="frame-area">
            <{component_tag} src="{item.src}" alt="{item.alt}" time="12:30" style="--frame-width: {frame_width_expr};"></{component_tag}>
        </div>
    </div>
    <script type="module">
{component_source}
    </script>
</body>
</html>
"""
    return html


def compute_feature_shot_width(device: str, slot_index: int) -> int:
    base = 230 if device == "phone" else 330
    if slot_index == 1:
        base += 30
    elif slot_index == 0:
        base -= 10
    return base


def build_feature_graphic_document(
    caption: str,
    subline: str,
    shots: Sequence[FeatureGraphicShot],
    component_source: str,
) -> str:
    safe_caption = escape(caption)
    safe_subline = escape(subline)
    shot_markup: List[str] = []
    for idx, shot in enumerate(shots):
        slot_class = f"slot-{idx + 1}"
        width = compute_feature_shot_width(shot.device, idx)
        time_attr = ' time="12:30"' if shot.component == "pc-screenshot-frame" else ""
        shot_markup.append(
            dedent(
                f"""
                <div class="feature-shot {slot_class} device-{shot.device}" style="--shot-frame-width: {width}px;">
                    <{shot.component} src="{shot.image_data}" alt="{escape(shot.alt)}"{time_attr} style="--frame-width: var(--shot-frame-width);"></{shot.component}>
                </div>
                """
            ).strip()
        )
    shots_html = "\n".join(shot_markup)
    html = f"""<!doctype html>
<html lang="en">
<head>
    <meta charset="utf-8">
    <title>{safe_caption}</title>
    <style>
        :root {{
            font-family: 'Space Grotesk', 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
            color: #f8fafc;
        }}

        * {{
            box-sizing: border-box;
        }}

        body {{
            margin: 0;
            width: {FEATURE_GRAPHIC_SIZE[0]}px;
            height: {FEATURE_GRAPHIC_SIZE[1]}px;
            background:
                radial-gradient(circle at 10% 10%, rgba(0,255,136,0.15), transparent 50%),
                radial-gradient(circle at 80% 0%, rgba(56,189,248,0.18), transparent 40%),
                linear-gradient(130deg, #01030c, #041129 60%, #050a18);
        }}

        .feature-canvas {{
            width: 100%;
            height: 100%;
            padding: 40px 50px;
            display: flex;
            align-items: stretch;
            justify-content: space-between;
            gap: 30px;
        }}

        .copy-block {{
            flex: 0 0 40%;
            display: flex;
            flex-direction: column;
            justify-content: center;
            gap: 16px;
        }}

        .copy-block .eyebrow {{
            letter-spacing: 0.3em;
            text-transform: uppercase;
            font-size: 0.85rem;
            color: rgba(248, 250, 252, 0.7);
        }}

        .copy-block h1 {{
            font-size: 3rem;
            line-height: 1.1;
            margin: 0;
        }}

        .copy-block .subline {{
            font-size: 1.05rem;
            color: rgba(248, 250, 252, 0.75);
        }}

        .shots {{
            flex: 1;
            position: relative;
            height: 100%;
            max-width: 660px;
        }}

        .feature-shot {{
            position: absolute;
            width: var(--shot-frame-width, 280px);
            padding: 14px;
            border-radius: 32px;
            background: rgba(2, 6, 20, 0.92);
            box-shadow:
                0 35px 90px rgba(0, 0, 0, 0.65),
                0 12px 35px rgba(15, 118, 110, 0.24);
            border: 1px solid rgba(255, 255, 255, 0.08);
        }}

        .feature-shot pc-screenshot-frame,
        .feature-shot pc-tablet-frame {{
            width: 100%;
            display: block;
        }}

        .feature-shot.slot-1 {{
            top: 90px;
            left: 20px;
            transform: rotate(-8deg);
        }}

        .feature-shot.slot-2 {{
            top: 20px;
            left: 210px;
            transform: rotate(1.5deg);
            z-index: 3;
        }}

        .feature-shot.slot-3 {{
            top: 120px;
            left: 360px;
            transform: rotate(9deg);
        }}
    </style>
</head>
<body>
    <div class="feature-canvas">
        <div class="copy-block">
            <span class="eyebrow">Portacode</span>
            <h1>{safe_caption}</h1>
            <p class="subline">{safe_subline}</p>
        </div>
        <div class="shots">
            {shots_html}
        </div>
    </div>
    <script type="module">
{component_source}
    </script>
</body>
</html>
"""
    return html


async def render_assets(
    browser: Browser,
    items: Sequence[ShowcaseItem],
    meta: DeviceMeta,
    output_dir: Path,
    component_source: str,
    width: int,
    height: int,
    scale: float,
    frame_width_expr: str,
    orientation: str,
) -> List[Path]:
    """Render each showcase item to a framed screenshot with Playwright."""
    output_dir.mkdir(parents=True, exist_ok=True)
    saved_paths: List[Path] = []

    for index, item in enumerate(items, start=1):
        page: Page = await browser.new_page(
            viewport={"width": width, "height": height},
            device_scale_factor=scale,
        )
        html = build_html_document(
            item,
            meta.component,
            component_source,
            frame_width_expr,
            orientation,
        )
        await page.set_content(html, wait_until="load")
        await page.wait_for_timeout(400)

        filename = f"{item.device}_{index:02d}_{slugify(item.caption)}.png"
        target = output_dir / filename
        await page.screenshot(path=str(target), full_page=True, timeout=60000)
        saved_paths.append(target)
        print(f"[+] Saved {target}")
        await page.close()
    return saved_paths


def build_feature_shots_payload(selection: FeatureGraphicInput) -> tuple[List[FeatureGraphicShot], str]:
    shots: List[FeatureGraphicShot] = []
    ordered_components: List[str] = []
    seen: set[str] = set()
    for shot in selection.shots:
        data_url = image_to_data_url(shot.asset_path)
        shots.append(
            FeatureGraphicShot(
                device=shot.device,
                device_label=shot.device_label,
                caption=shot.caption,
                alt=shot.alt,
                component=shot.component,
                image_data=data_url,
            )
        )
        if shot.component not in seen:
            seen.add(shot.component)
            ordered_components.append(shot.component)
    component_source = "\n\n".join(load_component_source(tag) for tag in ordered_components)
    return shots, component_source


async def render_feature_graphic(
    browser: Browser,
    selection: FeatureGraphicInput,
    output_dir: Path,
    scale: float,
) -> List[Path]:
    width, height = FEATURE_GRAPHIC_SIZE
    shots, component_source = build_feature_shots_payload(selection)
    html = build_feature_graphic_document(selection.caption, selection.subline, shots, component_source)
    page: Page = await browser.new_page(
        viewport={"width": width, "height": height},
        device_scale_factor=scale,
    )
    await page.set_content(html, wait_until="load")
    await page.wait_for_timeout(400)
    feature_dir = output_dir / "feature_graphic"
    feature_dir.mkdir(parents=True, exist_ok=True)
    filename = f"feature_graphic_{slugify(selection.caption)}.png"
    target = feature_dir / filename
    await page.screenshot(path=str(target), full_page=True, timeout=60000)
    await page.close()
    print(f"[+] Saved {target}")
    return [target]


def inline_module_source(path: Path, root: bool = True, visited=None) -> str:
    if visited is None:
        visited = set()

    path = path.resolve()
    if path in visited:
        return ""

    visited.add(path)
    content = path.read_text(encoding="utf-8").splitlines()
    output_lines: List[str] = []

    for line in content:
        match = IMPORT_PATTERN.match(line)
        if match:
            indent, _, target = match.groups()
            if target.startswith("."):
                dep_path = (path.parent / target).resolve()
                dep_source = inline_module_source(dep_path, root=False, visited=visited)
                if dep_source:
                    output_lines.append(f"{indent}// Inlined from {dep_path}")
                    output_lines.append(dep_source)
                    output_lines.append(f"{indent}// End inline {dep_path}")
                continue
        if not root and line.strip().startswith("export default"):
            continue
        if not root and line.strip().startswith("export "):
            cleaned = re.sub(r"^(\s*)export\s+", r"\1", line, count=1)
            output_lines.append(cleaned)
        else:
            output_lines.append(line)

    return "\n".join(output_lines)


def load_component_source(component_tag: str) -> str:
    module_path = COMPONENT_MODULES.get(component_tag)
    if not module_path:
        raise ValueError(f"No module mapping for component '{component_tag}'.")
    if not module_path.exists():
        raise FileNotFoundError(f"Component module not found: {module_path}")
    return inline_module_source(module_path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Generate framed Google Play Store screenshots from homepage assets.",
    )
    parser.add_argument(
        "--device",
        choices=["phone", "tablet7", "tablet10", "all"],
        default="phone",
        help="Device key to render (default: phone).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("dist/google_play_assets"),
        help="Directory to store generated screenshots.",
    )
    parser.add_argument(
        "--feature-graphic",
        action="store_true",
        help="Generate the feature graphic (1024x500) instead of device screenshots.",
    )
    parser.add_argument(
        "--feature-picks",
        help="Comma-separated screenshot indexes to include in the feature graphic (only used with --feature-graphic).",
    )
    parser.add_argument(
        "--feature-caption",
        help="Override caption text for the generated feature graphic.",
    )
    parser.add_argument(
        "--width",
        type=int,
        default=1440,
        help="Viewport width in pixels (default 1440).",
    )
    parser.add_argument(
        "--height",
        type=int,
        default=2560,
        help="Viewport height in pixels (default 2560).",
    )
    parser.add_argument(
        "--scale",
        type=float,
        default=2.0,
        help="Device scale factor for crisp output (default 2.0).",
    )
    parser.add_argument(
        "--frame-width",
        default=None,
        help="Override CSS width expression for frames (e.g. 'min(80vw, 1500px)').",
    )
    parser.add_argument(
        "--headed",
        dest="headless",
        action="store_false",
        help="Run browser in headed mode for debugging.",
    )
    parser.set_defaults(headless=True)
    return parser.parse_args()


async def main_async(args: argparse.Namespace) -> List[Path]:
    if async_playwright is None:
        raise RuntimeError("Playwright is not installed. Run: pip install playwright")

    parser = HomeShowcaseParser(TEMPLATE_PATH)
    devices = list(parser.showcase_data.keys())
    if not devices:
        raise RuntimeError("No showcase data found in homepage template.")

    feature_selection: FeatureGraphicInput | None = None
    if args.feature_graphic:
        feature_selection = prepare_feature_graphic_input(parser, args)
        targets: Iterable[str] = []
    elif args.device == "all":
        targets = parser.showcase_data.keys()
    else:
        targets = [args.device]

    generated: List[Path] = []

    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=args.headless)
        try:
            if args.feature_graphic:
                if not feature_selection:
                    raise RuntimeError("Feature graphic selection could not be prepared.")
                saved = await render_feature_graphic(
                    browser=browser,
                    selection=feature_selection,
                    output_dir=args.output_dir,
                    scale=args.scale,
                )
                generated.extend(saved)
            else:
                for device in targets:
                    if device not in parser.showcase_data:
                        print(f"[!] Skipping unknown device '{device}' (not in template)")
                        continue
                    device_meta = parser.device_meta.get(device)
                    if not device_meta:
                        print(f"[!] Missing device config for '{device}', skipping.")
                        continue
                    component_source = load_component_source(device_meta.component)

                    processed_items: List[ShowcaseItem] = []
                    for item in parser.showcase_data[device]:
                        asset_path = parser.resolve_static_path(item.src)
                        processed_items.append(
                            ShowcaseItem(
                                device=item.device,
                                src=image_to_data_url(asset_path),
                                alt=item.alt,
                                caption=item.caption,
                                description=item.description,
                            )
                        )

                    viewport_width, viewport_height = DEVICE_VIEWPORT.get(
                        device,
                        (args.width, args.height),
                    )
                    frame_width_expr = resolve_frame_width(device, viewport_width, args.frame_width)
                    orientation = DEVICE_ORIENTATION.get(device, DEFAULT_ORIENTATION)
                    device_dir = args.output_dir / device
                    saved = await render_assets(
                        browser=browser,
                        items=processed_items,
                        meta=device_meta,
                        output_dir=device_dir,
                        component_source=component_source,
                        width=viewport_width,
                        height=viewport_height,
                        scale=args.scale,
                        frame_width_expr=frame_width_expr,
                        orientation=orientation,
                    )
                    generated.extend(saved)
        finally:
            await browser.close()

    return generated


def main() -> None:
    args = parse_args()
    try:
        generated_paths = asyncio.run(main_async(args))
    except KeyboardInterrupt:
        print("Interrupted by user.")
        return
    except Exception as exc:  # pragma: no cover - CLI diagnostics
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    if not generated_paths:
        print("No screenshots were generated.")
        return

    print("\nGeneration complete. Assets:")
    for path in generated_paths:
        print(f" - {path}")


if __name__ == "__main__":
    main()

# /// script
# dependencies = ["pillow>=11"]
# ///
"""Render profile cards, README galleries, and full-list Markdown pages."""

import html
import json
import os
import re
import shutil
import subprocess
import tempfile
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data/profile.json"
CARD_DIR = ROOT / "assets/cards"
TEMPLATE_PATH = ROOT / "templates/cards.html"
CSS_PATH = ROOT / "templates/cards.css"
README_START = "<!-- profile-cards:start -->"
README_END = "<!-- profile-cards:end -->"

SECTIONS = {
    "open_source": {
        "title": "Open source",
        "accent": "#2f81f7",
        "output": "open-source",
        "page": "OPEN_SOURCE.md",
        "view": "View all open-source work",
        "introduction": "Packages, developer tools, research code, and applications I maintain or created.",
    },
    "papers": {
        "title": "Research",
        "accent": "#00a6c8",
        "output": "research",
        "page": "PAPERS.md",
        "view": "View all papers",
        "introduction": "Published papers and preprints, listed with their verified venue and year.",
    },
    "talks": {
        "title": "Public speaking",
        "accent": "#d97706",
        "output": "talks",
        "page": "TALKS.md",
        "view": "View all talks",
        "introduction": "Completed panels, talks, and workshops with links to the original event posts.",
    },
    "products": {
        "title": "Products",
        "accent": "#db5b75",
        "output": "products",
        "page": "PRODUCTS.md",
        "view": "View all products",
        "introduction": "Web products I have built and operate.",
    },
}

LAYOUTS = {
    "desktop": {"width": 280, "padding": 6, "media": 160, "content": 154},
    "mobile": {"width": 360, "padding": 8, "media": 210, "content": 184},
}

METRIC_LABELS = {
    "stars": "stars",
    "forks": "forks",
    "downloads_total": "downloads",
    "downloads_month": "downloads · month",
}


def card_label(section: str, item: dict) -> str:
    """Build the small category label shown above a card title.

    Args:
        section (str): Profile section key.
        item (dict): Card data.

    Returns:
        (str): Card label.
    """
    if section == "papers":
        return f"{item['venue']} · {item['year']}"
    if section == "talks":
        return f"{item['venue']} · {item['date'][:4]}"
    return item["kind"]


def card_stats(section: str, item: dict) -> list[str]:
    """Build the metric strings shown at the bottom of a card.

    Args:
        section (str): Profile section key.
        item (dict): Card data.

    Returns:
        (list[str]): Metric labels.
    """
    if section == "open_source":
        stats = []
        for key in item["card_metrics"]:
            value = item["metrics"][key]
            stats.append(
                f"{value:,} {METRIC_LABELS[key]}"
                if isinstance(value, int)
                else f"{value} {METRIC_LABELS[key]}"
            )
        return stats
    if section == "papers":
        citations = item["metrics"]["citations"]
        return [f"{citations:,} citation{'s' if citations != 1 else ''}"]
    if section == "talks":
        return [date.fromisoformat(item["date"]).strftime("%b %Y")]
    return ["Live web app"]


def render_card(section: str, item: dict) -> str:
    """Render one card as HTML.

    Args:
        section (str): Profile section key.
        item (dict): Card data.

    Returns:
        (str): Card HTML.
    """
    image_uri = (ROOT / item["image"]).resolve().as_uri()
    card_class = (
        "card page-top"
        if section == "papers"
        else f"card {item['image_fit']}"
        if item.get("image_fit")
        else "card"
    )
    stats = "".join(
        f'<span class="stat">{html.escape(value)}</span>'
        for value in card_stats(section, item)
    )
    return (
        f'<article class="{card_class}">'
        f'<div class="media"><img src="{image_uri}" alt=""></div>'
        '<div class="content">'
        f'<p class="label">{html.escape(card_label(section, item))}</p>'
        f'<h2 class="title">{html.escape(item["name"])}</h2>'
        f'<p class="description">{html.escape(re.sub(r"\[([^]]+)]\([^)]+\)", r"\1", item.get("description", item.get("title", ""))))}</p>'
        f'<div class="stats">{stats}</div>'
        "</div></article>"
    )


def card_slug(value: str) -> str:
    """Convert a card name to a stable filename segment.

    Args:
        value (str): Display name to convert.

    Returns:
        (str): Lowercase hyphenated filename segment.
    """
    return re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")


def chrome_path() -> str:
    """Find the Chrome executable used for deterministic screenshots.

    Returns:
        (str): Chrome executable path.
    """
    candidates = [
        os.environ.get("CHROME_PATH"),
        shutil.which("google-chrome"),
        shutil.which("google-chrome-stable"),
        shutil.which("chromium"),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    ]
    executable = next(
        (path for path in candidates if path and Path(path).exists()), None
    )
    if not executable:
        raise FileNotFoundError("Chrome was not found; set CHROME_PATH")
    return executable


def render_screenshot(job: tuple[str, Path, Path, int, int]) -> None:
    """Render one prepared card page with headless Chrome.

    Args:
        job (tuple): Chrome path, input, output, width, and height.
    """
    executable, input_path, output_path, width, height = job
    png_path = input_path.with_suffix(".png")
    subprocess.run(
        [
            executable,
            "--headless=new",
            "--disable-gpu",
            "--disable-logging",
            "--hide-scrollbars",
            "--log-level=3",
            "--no-sandbox",
            "--allow-file-access-from-files",
            "--force-device-scale-factor=1",
            f"--window-size={width},{height}",
            f"--screenshot={png_path}",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=1000",
            input_path.as_uri(),
        ],
        check=True,
    )
    with Image.open(png_path) as image:
        image.save(output_path, "WEBP", quality=88, method=6)
    png_path.unlink()


def render_cards(data: dict) -> None:
    """Render every featured card in light, dark, desktop, and mobile forms.

    Args:
        data (dict): Canonical profile data.
    """
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    template = TEMPLATE_PATH.read_text()
    css = CSS_PATH.read_text()
    executable = chrome_path()
    for pattern in ("*.png", "*.webp"):
        for path in CARD_DIR.glob(pattern):
            path.unlink()

    with tempfile.TemporaryDirectory(prefix="fcakyon-cards-") as temporary:
        temporary_dir = Path(temporary)
        jobs = []
        for section, config in SECTIONS.items():
            for item in (entry for entry in data[section] if entry.get("featured")):
                slug = card_slug(item["name"])
                for layout_name, layout in LAYOUTS.items():
                    height = 2 * layout["padding"] + layout["media"] + layout["content"]
                    for theme in ("light", "dark"):
                        values = {
                            "theme": theme,
                            "css": css,
                            "width": str(layout["width"]),
                            "height": str(height),
                            "padding": str(layout["padding"]),
                            "accent": config["accent"],
                            "media_height": str(layout["media"]),
                            "content_height": str(layout["content"]),
                            "layout": layout_name,
                            "card": render_card(section, item),
                        }
                        page = template
                        for key, value in values.items():
                            page = page.replace(f"{{{{{key}}}}}", value)
                        name = f"{config['output']}-{slug}-{layout_name}-{theme}"
                        input_path = temporary_dir / f"{name}.html"
                        output_path = CARD_DIR / f"{name}.webp"
                        input_path.write_text(page)
                        jobs.append(
                            (
                                executable,
                                input_path,
                                output_path,
                                layout["width"],
                                height,
                            )
                        )
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(render_screenshot, jobs))


def render_picture(config: dict, item: dict) -> str:
    """Render one linked, theme-aware README card.

    Args:
        config (dict): Section rendering configuration.
        item (dict): Featured card data.

    Returns:
        (str): Linked HTML picture element.
    """
    prefix = f"assets/cards/{config['output']}-{card_slug(item['name'])}"
    return (
        f'<a href="{html.escape(item["url"], quote=True)}"><picture>'
        f'<source media="(max-width: 700px) and (prefers-color-scheme: dark)" srcset="{prefix}-mobile-dark.webp 2.1x">'
        f'<source media="(max-width: 700px)" srcset="{prefix}-mobile-light.webp 2.1x">'
        f'<source media="(max-width: 1100px) and (prefers-color-scheme: dark)" srcset="{prefix}-desktop-dark.webp 1.65x">'
        f'<source media="(max-width: 1100px)" srcset="{prefix}-desktop-light.webp 1.65x">'
        f'<source media="(prefers-color-scheme: dark)" srcset="{prefix}-desktop-dark.webp 1.45x">'
        f'<source media="(min-width: 1101px)" srcset="{prefix}-desktop-light.webp 1.45x">'
        f'<img src="{prefix}-desktop-light.webp" alt="{html.escape(item["name"], quote=True)}">'
        "</picture></a>"
    )


def render_readme(data: dict) -> None:
    """Replace the generated README gallery between its markers.

    Args:
        data (dict): Canonical profile data.
    """
    path = ROOT / "README.md"
    readme = path.read_text()
    before, marker, remainder = readme.partition(README_START)
    _, end, after = remainder.partition(README_END)
    if not marker or not end:
        raise RuntimeError("README card markers are missing")

    lines = []
    for section, config in SECTIONS.items():
        lines.extend([f"## {config['title'].lower()}", "", "<p>"])
        lines.extend(
            render_picture(config, item)
            for item in data[section]
            if item.get("featured")
        )
        lines.extend(["</p>", "", f"[{config['view']}]({config['page']})", ""])
    gallery = "\n".join(lines).rstrip()
    path.write_text(f"{before}{README_START}\n\n{gallery}\n\n{README_END}{after}")


def render_markdown(data: dict) -> None:
    """Render the four full-list Markdown pages.

    Args:
        data (dict): Canonical profile data.
    """
    for section, config in SECTIONS.items():
        lines = [
            f"# {config['title']}",
            "",
            "[Back to profile](README.md)",
            "",
            config["introduction"],
            "",
        ]
        for item in data[section]:
            if section == "papers":
                citations = item["metrics"]["citations"]
                count = f"{citations:,} citation{'s' if citations != 1 else ''}"
                lines.append(
                    f"- [{item['title']}]({item['url']}) ({item['venue']}, {item['year']}, {count})"
                )
            elif section == "talks":
                event_date = date.fromisoformat(item["date"]).strftime("%B %Y")
                lines.append(
                    f"- [{item['title']}]({item['url']}) ({item['venue']}, {event_date}): {item['description']}"
                )
            elif section == "open_source":
                stars = item.get("metrics", {}).get("stars")
                total = f" ({stars:,} stars)" if stars is not None else ""
                lines.append(
                    f"- [{item['name']}]({item['url']}){total}: {item['description']}"
                )
            else:
                lines.append(
                    f"- [{item['name']}]({item['url']}): {item['description']}"
                )
        (ROOT / config["page"]).write_text("\n".join(lines) + "\n")


def main() -> None:
    """Render cards and Markdown from the canonical data file."""
    data = json.loads(DATA_PATH.read_text())
    render_cards(data)
    render_readme(data)
    render_markdown(data)
    print("Rendered profile cards and Markdown pages")


if __name__ == "__main__":
    main()

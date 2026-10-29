# /// script
# dependencies = ["pillow>=11"]
# ///
"""Render profile cards, README galleries, and full-list Markdown pages.

Cards and their source images live on cdn.akyonlabs.com, backed by the
akyonlabs-public-assets R2 bucket, not in git. Point a featured item's `image` at a
local file and the next render uploads it and stores its CDN URL. Uploads use
CLOUDFLARE_API_TOKEN, a repository secret in CI, or locally the token file
~/.config/akyonlabs/cloudflare-r2-token.
"""

import hashlib
import html
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
DATA_PATH = ROOT / "data/profile.json"
TEMPLATE_PATH = ROOT / "templates/cards.html"
CSS_PATH = ROOT / "templates/cards.css"
README_START = "<!-- profile-cards:start -->"
README_END = "<!-- profile-cards:end -->"
R2_ACCOUNT = "24b962faa5fe5cb7bf6fd87a2fdcbace"
R2_BUCKET = "akyonlabs-public-assets"
R2_TOKEN = (
    os.environ.get("CLOUDFLARE_API_TOKEN")
    or (Path.home() / ".config/akyonlabs/cloudflare-r2-token").read_text().strip()
)
CDN_URL = "https://cdn.akyonlabs.com"
CONTENT_TYPES = {".jpg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}

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
    "patents": {
        "title": "Patents",
        "accent": "#16a34a",
        "output": "patents",
        "page": "PATENTS.md",
        "view": "View all patents",
        "introduction": "Granted Turkish patents and PCT applications, linked to their public records.",
    },
    "peer_review": {
        "title": "Peer review",
        "accent": "#7c3aed",
        "output": "peer-review",
        "page": "PEER_REVIEW.md",
        "view": "View all peer review",
        "introduction": "Journals I review manuscripts for, linked to their impact factor pages.",
    },
    "products": {
        "title": "Products",
        "accent": "#db5b75",
        "output": "products",
        "page": "PRODUCTS.md",
        "view": "View all products",
        "introduction": "Web products I have built and operate.",
    },
    "media": {
        "title": "Media",
        "accent": "#0891b2",
        "output": "media",
        "page": "MEDIA.md",
        "view": "View all media coverage",
        "introduction": "News articles and interviews about my work, linked to the original sources.",
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
    if section == "patents":
        return f"{item['assignee']} · {item['year']}"
    if section in ("talks", "media"):
        return f"{item['venue']} · {item['date'][:4]}"
    if section == "peer_review":
        return item["publisher"]
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
    if section == "patents":
        return [item["number"], item["status"]]
    if section in ("talks", "media"):
        return [date.fromisoformat(item["date"]).strftime("%b %Y")]
    if section == "peer_review":
        return [f"Impact factor {item['impact_factor']}", item["quartile"]]
    return ["Live web app"]


def render_card(section: str, item: dict) -> str:
    """Render one card as HTML.

    Args:
        section (str): Profile section key.
        item (dict): Card data.

    Returns:
        (str): Card HTML.
    """
    card_class = (
        "card page-top"
        if section in ("papers", "patents", "peer_review")
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
        f'<div class="media"><img src="{item["image"]}" alt=""></div>'
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


def upload(path: Path) -> str:
    """Upload a file to the Akyon Labs CDN under its content hash.

    Args:
        path (Path): Local file to upload.

    Returns:
        (str): Public CDN URL of the file.
    """
    body = path.read_bytes()
    key = f"fcakyon/static/{hashlib.sha256(body).hexdigest()[:16]}/{path.name}"
    urllib.request.urlopen(
        urllib.request.Request(
            f"https://api.cloudflare.com/client/v4/accounts/{R2_ACCOUNT}/r2/buckets/{R2_BUCKET}/objects/{key}",
            data=body,
            method="PUT",
            headers={
                "Authorization": f"Bearer {R2_TOKEN}",
                "Content-Type": CONTENT_TYPES[path.suffix],
                "Cache-Control": "public, max-age=31536000, immutable",
            },
        )
    ).close()
    return f"{CDN_URL}/{key}"


def render_screenshot(
    executable: str, input_path: Path, width: int, height: int
) -> Path:
    """Render one prepared card page with headless Chrome.

    Args:
        executable (str): Chrome executable path.
        input_path (Path): Prepared card page.
        width (int): Window width in pixels.
        height (int): Window height in pixels.

    Returns:
        (Path): Rendered WebP card next to the input page.
    """
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
            "--force-device-scale-factor=1",
            f"--window-size={width},{height}",
            f"--screenshot={png_path}",
            "--run-all-compositor-stages-before-draw",
            "--virtual-time-budget=1000",
            input_path.as_uri(),
        ],
        check=True,
    )
    output_path = input_path.with_suffix(".webp")
    with Image.open(png_path) as image:
        image.save(output_path, "WEBP", quality=88, method=6)
    return output_path


def render_cards(data: dict) -> dict[str, str]:
    """Render and upload every featured card in light, dark, desktop, and mobile forms.

    A featured item whose `image` is still a local path is uploaded first, and its
    field is set to the CDN URL.

    Args:
        data (dict): Canonical profile data, updated in place with uploaded image URLs.

    Returns:
        (dict[str, str]): CDN URL of each card, keyed by card name.
    """
    template = TEMPLATE_PATH.read_text()
    css = CSS_PATH.read_text()
    executable = chrome_path()

    with tempfile.TemporaryDirectory(prefix="fcakyon-cards-") as temporary:
        temporary_dir = Path(temporary)
        jobs = {}
        for section, config in SECTIONS.items():
            for item in (entry for entry in data[section] if entry.get("featured")):
                if not item["image"].startswith(CDN_URL):
                    item["image"] = upload(ROOT / item["image"])
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
                        input_path.write_text(page)
                        jobs[name] = (input_path, layout["width"], height)
        with ThreadPoolExecutor(max_workers=4) as executor:
            urls = executor.map(
                lambda job: upload(render_screenshot(executable, *job)), jobs.values()
            )
            return dict(zip(jobs, urls))


def render_picture(config: dict, item: dict, cards: dict[str, str]) -> str:
    """Render one linked, theme-aware README card.

    Args:
        config (dict): Section rendering configuration.
        item (dict): Featured card data.
        cards (dict[str, str]): CDN URL of each card, keyed by card name.

    Returns:
        (str): Linked HTML picture element.
    """
    prefix = f"{config['output']}-{card_slug(item['name'])}"
    return (
        f'<a href="{html.escape(item["url"], quote=True)}"><picture>'
        f'<source media="(max-width: 700px) and (prefers-color-scheme: dark)" srcset="{cards[prefix + "-mobile-dark"]}">'
        f'<source media="(max-width: 700px)" srcset="{cards[prefix + "-mobile-light"]}">'
        f'<source media="(prefers-color-scheme: dark)" srcset="{cards[prefix + "-desktop-dark"]}">'
        f'<img src="{cards[prefix + "-desktop-light"]}" width="170" alt="{html.escape(item["name"], quote=True)}">'
        "</picture></a>"
    )


def render_readme(data: dict, cards: dict[str, str]) -> None:
    """Replace the generated README gallery between its markers.

    Args:
        data (dict): Canonical profile data.
        cards (dict[str, str]): CDN URL of each card, keyed by card name.
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
            render_picture(config, item, cards)
            for item in data[section]
            if item.get("featured")
        )
        lines.extend(["</p>", "", f"[{config['view']}]({config['page']})", ""])
    gallery = "\n".join(lines).rstrip()
    path.write_text(f"{before}{README_START}\n\n{gallery}\n\n{README_END}{after}")


def render_markdown(data: dict) -> None:
    """Render the full-list Markdown pages.

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
            elif section == "patents":
                lines.append(
                    f"- [{item['title']}]({item['url']}) ({item['number']}, {item['assignee']}, {item['status'].lower()} {item['year']})"
                )
            elif section in ("talks", "media"):
                event_date = date.fromisoformat(item["date"]).strftime("%B %Y")
                lines.append(
                    f"- [{item['title']}]({item['url']}) ({item['venue']}, {event_date}): {item['description']}"
                )
            elif section == "peer_review":
                lines.append(
                    f"- [{item['name']}]({item['url']}) ({item['publisher']}, impact factor {item['impact_factor']}, {item['quartile']}): {item['description']}"
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
    render_readme(data, render_cards(data))
    DATA_PATH.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n")
    render_markdown(data)
    print("Rendered profile cards and Markdown pages")


if __name__ == "__main__":
    main()

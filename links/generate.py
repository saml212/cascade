#!/usr/bin/env python3
"""Generate the links page (link-in-bio) from config/config.toml.

Usage:
    python -m links.generate              # outputs to links/index.html
    python -m links.generate -o out.html  # custom output path
"""

import argparse
import html
import re
import sys
from datetime import datetime
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib  # Python < 3.11


# SVG icons for each supported platform (brand colors baked in)
PLATFORM_ICONS = {
    "spotify": (
        "Spotify",
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M12 0C5.4 0 0 5.4 0 12s5.4 12 12 12 12-5.4 12-12S18.66 0 12 0zm5.521 17.34c-.24.359-.66.48-1.021.24-2.82-1.74-6.36-2.101-10.561-1.141-.418.122-.779-.179-.899-.539-.12-.421.18-.78.54-.9 4.56-1.021 8.52-.6 11.64 1.32.42.18.479.659.301 1.02zm1.44-3.3c-.301.42-.841.6-1.262.3-3.239-1.98-8.159-2.58-11.939-1.38-.479.12-1.02-.12-1.14-.6-.12-.48.12-1.021.6-1.141C9.6 9.9 15 10.561 18.72 12.84c.361.181.54.78.241 1.2zm.12-3.36C15.24 8.4 8.82 8.16 5.16 9.301c-.6.179-1.2-.181-1.38-.721-.18-.601.18-1.2.72-1.381 4.26-1.26 11.28-1.02 15.721 1.621.539.3.719 1.02.419 1.56-.299.421-1.02.599-1.559.3z" fill="#1DB954"/>'
        "</svg>",
    ),
    "youtube": (
        "YouTube",
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M23.498 6.186a3.016 3.016 0 0 0-2.122-2.136C19.505 3.545 12 3.545 12 3.545s-7.505 0-9.377.505A3.017 3.017 0 0 0 .502 6.186C0 8.07 0 12 0 12s0 3.93.502 5.814a3.016 3.016 0 0 0 2.122 2.136c1.871.505 9.376.505 9.376.505s7.505 0 9.377-.505a3.015 3.015 0 0 0 2.122-2.136C24 15.93 24 12 24 12s0-3.93-.502-5.814zM9.545 15.568V8.432L15.818 12l-6.273 3.568z" fill="#FF0000"/>'
        "</svg>",
    ),
    "apple_podcasts": (
        "Apple Podcasts",
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M5.34 0A5.328 5.328 0 0 0 0 5.34v13.32A5.328 5.328 0 0 0 5.34 24h13.32A5.328 5.328 0 0 0 24 18.66V5.34A5.328 5.328 0 0 0 18.66 0zm6.525 2.568c4.988 0 8.94 3.764 9.098 8.397.027.753-.593 1.327-1.346 1.327h-.085c-.735 0-1.3-.58-1.363-1.313C17.988 7.498 15.24 4.9 11.865 4.9c-3.457 0-6.267 2.726-6.397 6.122-.015.396-.18.768-.47 1.042-.29.273-.672.414-1.063.396-.743-.035-1.322-.663-1.298-1.406C2.8 6.368 6.725 2.568 11.865 2.568zm.045 3.628a5.3 5.3 0 0 1 5.3 5.073c.037.75-.565 1.381-1.315 1.381h-.058c-.7 0-1.266-.54-1.332-1.238a2.984 2.984 0 0 0-2.595-2.7 2.98 2.98 0 0 0-3.31 2.466c-.1.688-.69 1.195-1.385 1.195h-.04c-.794 0-1.42-.686-1.326-1.474a5.3 5.3 0 0 1 4.66-4.703zm-.14 4.503a2.09 2.09 0 0 1 2.09 2.09c0 .554-.218 1.057-.57 1.43l.856 4.89c.132.753-.44 1.442-1.205 1.442h-2.39c-.764 0-1.336-.69-1.204-1.442l.856-4.89a2.075 2.075 0 0 1-.523-1.43 2.09 2.09 0 0 1 2.09-2.09z" fill="#9933CC"/>'
        "</svg>",
    ),
    "instagram": (
        "Instagram",
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        "<defs>"
        '<linearGradient id="ig" x1="0%" y1="100%" x2="100%" y2="0%">'
        '<stop offset="0%" stop-color="#FFDC80"/>'
        '<stop offset="25%" stop-color="#F77737"/>'
        '<stop offset="50%" stop-color="#E1306C"/>'
        '<stop offset="75%" stop-color="#C13584"/>'
        '<stop offset="100%" stop-color="#833AB4"/>'
        "</linearGradient>"
        "</defs>"
        '<path d="M12 2.163c3.204 0 3.584.012 4.85.07 3.252.148 4.771 1.691 4.919 4.919.058 1.265.069 1.645.069 4.849 0 3.205-.012 3.584-.069 4.849-.149 3.225-1.664 4.771-4.919 4.919-1.266.058-1.644.07-4.85.07-3.204 0-3.584-.012-4.849-.07-3.26-.149-4.771-1.699-4.919-4.92-.058-1.265-.07-1.644-.07-4.849 0-3.204.013-3.583.07-4.849.149-3.227 1.664-4.771 4.919-4.919 1.266-.057 1.645-.069 4.849-.069zM12 0C8.741 0 8.333.014 7.053.072 2.695.272.273 2.69.073 7.052.014 8.333 0 8.741 0 12c0 3.259.014 3.668.072 4.948.2 4.358 2.618 6.78 6.98 6.98C8.333 23.986 8.741 24 12 24c3.259 0 3.668-.014 4.948-.072 4.354-.2 6.782-2.618 6.979-6.98.059-1.28.073-1.689.073-4.948 0-3.259-.014-3.667-.072-4.947-.196-4.354-2.617-6.78-6.979-6.98C15.668.014 15.259 0 12 0zm0 5.838a6.162 6.162 0 1 0 0 12.324 6.162 6.162 0 0 0 0-12.324zM12 16a4 4 0 1 1 0-8 4 4 0 0 1 0 8zm6.406-11.845a1.44 1.44 0 1 0 0 2.881 1.44 1.44 0 0 0 0-2.881z" fill="url(#ig)"/>'
        "</svg>",
    ),
    "x": (
        "X",
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M18.244 2.25h3.308l-7.227 8.26 8.502 11.24H16.17l-5.214-6.817L4.99 21.75H1.68l7.73-8.835L1.254 2.25H8.08l4.713 6.231zm-1.161 17.52h1.833L7.084 4.126H5.117z" fill="#e4e4e7"/>'
        "</svg>",
    ),
    "tiktok": (
        "TikTok",
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M12.525.02c1.31-.02 2.61-.01 3.91-.02.08 1.53.63 3.09 1.75 4.17 1.12 1.11 2.7 1.62 4.24 1.79v4.03c-1.44-.05-2.89-.35-4.2-.97-.57-.26-1.1-.59-1.62-.93-.01 2.92.01 5.84-.02 8.75-.08 1.4-.54 2.79-1.35 3.94-1.31 1.92-3.58 3.17-5.91 3.21-1.43.08-2.86-.31-4.08-1.03-2.02-1.19-3.44-3.37-3.65-5.71-.02-.5-.03-1-.01-1.49.18-1.9 1.12-3.72 2.58-4.96 1.66-1.44 3.98-2.13 6.15-1.72.02 1.48-.04 2.96-.04 4.44-.99-.32-2.15-.23-3.02.37-.63.41-1.11 1.04-1.36 1.75-.21.51-.15 1.07-.14 1.61.24 1.64 1.82 3.02 3.5 2.87 1.12-.01 2.19-.66 2.77-1.61.19-.33.4-.67.41-1.06.1-1.79.06-3.57.07-5.36.01-4.03-.01-8.05.02-12.07z" fill="#e4e4e7"/>'
        "</svg>",
    ),
    "iheartradio": (
        "iHeartRadio",
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M12 0C5.373 0 0 5.373 0 12s5.373 12 12 12 12-5.373 12-12S18.627 0 12 0zm0 4.151c1.846 0 3.556.63 4.908 1.687l-.744.965A6.454 6.454 0 0 0 12 5.369a6.454 6.454 0 0 0-4.164 1.434l-.744-.965A7.628 7.628 0 0 1 12 4.151zm-6.294 3.12l.756.953A5.233 5.233 0 0 0 4.76 12c0 2.9 2.34 5.24 5.24 5.24h4c2.9 0 5.24-2.34 5.24-5.24 0-1.432-.568-2.732-1.702-3.776l.756-.953A6.44 6.44 0 0 1 20.46 12c0 3.56-2.88 6.46-6.46 6.46h-4C6.44 18.46 3.56 15.56 3.56 12a6.44 6.44 0 0 1 2.146-4.729zM12 8.5a3.5 3.5 0 0 1 3.5 3.5c0 1.246-.67 2.34-1.667 2.943L12 19.849l-1.833-4.906A3.49 3.49 0 0 1 8.5 12 3.5 3.5 0 0 1 12 8.5zm0 2a1.5 1.5 0 1 0 0 3 1.5 1.5 0 0 0 0-3z" fill="#C6002B"/>'
        "</svg>",
    ),
    "github": (
        "GitHub",
        '<svg viewBox="0 0 24 24" xmlns="http://www.w3.org/2000/svg">'
        '<path d="M12 .297c-6.63 0-12 5.373-12 12 0 5.303 3.438 9.8 8.205 11.385.6.113.82-.258.82-.577 0-.285-.01-1.04-.015-2.04-3.338.724-4.042-1.61-4.042-1.61C4.422 18.07 3.633 17.7 3.633 17.7c-1.087-.744.084-.729.084-.729 1.205.084 1.838 1.236 1.838 1.236 1.07 1.835 2.809 1.305 3.495.998.108-.776.417-1.305.76-1.605-2.665-.3-5.466-1.332-5.466-5.93 0-1.31.465-2.38 1.235-3.22-.135-.303-.54-1.523.105-3.176 0 0 1.005-.322 3.3 1.23.96-.267 1.98-.399 3-.405 1.02.006 2.04.138 3 .405 2.28-1.552 3.285-1.23 3.285-1.23.645 1.653.24 2.873.12 3.176.765.84 1.23 1.91 1.23 3.22 0 4.61-2.805 5.625-5.475 5.92.42.36.81 1.096.81 2.22 0 1.606-.015 2.896-.015 3.286 0 .315.21.69.825.57C20.565 22.092 24 17.592 24 12.297c0-6.627-5.373-12-12-12" fill="#e4e4e7"/>'
        "</svg>",
    ),
}

# Display order for platforms
PLATFORM_ORDER = [
    "spotify",
    "apple_podcasts",
    "youtube",
    "instagram",
    "x",
    "tiktok",
    "iheartradio",
    "github",
]


def extract_spotify_id(url):
    """Extract Spotify show ID from URL for embed player."""
    m = re.search(r"show/([a-zA-Z0-9]+)", url)
    return m.group(1) if m else None


def build_link_block(platform, url):
    """Build one <a> link block for the given platform."""
    label, svg = PLATFORM_ICONS[platform]
    escaped_url = html.escape(url, quote=True)
    return (
        f'      <a href="{escaped_url}" class="link" target="_blank" rel="noopener">\n'
        f'        <span class="link-icon">{svg}</span>\n'
        f"        {label}\n"
        f"      </a>"
    )


def generate(config_path, template_path, output_path):
    with open(config_path, "rb") as f:
        config = tomllib.load(f)

    podcast = config.get("podcast", {})
    links_cfg = podcast.get("links", {})

    title = podcast.get("title", "My Podcast")
    artwork_url = podcast.get("artwork_url", "")
    tagline = links_cfg.get("tagline", "")

    # Build link blocks for platforms that have a non-empty URL
    link_blocks = []
    for platform in PLATFORM_ORDER:
        url = links_cfg.get(platform, "").strip()
        if url and platform in PLATFORM_ICONS:
            link_blocks.append(build_link_block(platform, url))

    # Spotify embed
    spotify_url = links_cfg.get("spotify", "").strip()
    spotify_id = extract_spotify_id(spotify_url) if spotify_url else None
    if spotify_id:
        spotify_embed = (
            '    <div class="spotify-embed">\n'
            "      <iframe\n"
            f'        src="https://open.spotify.com/embed/show/{spotify_id}?utm_source=generator&theme=0"\n'
            '        allowfullscreen\n'
            '        allow="autoplay; clipboard-write; encrypted-media; fullscreen; picture-in-picture"\n'
            '        loading="lazy"\n'
            "      ></iframe>\n"
            "    </div>"
        )
    else:
        spotify_embed = ""

    with open(template_path) as f:
        template = f.read()

    result = template.replace("{{title}}", html.escape(title))
    result = result.replace("{{tagline}}", html.escape(tagline))
    result = result.replace("{{artwork_url}}", html.escape(artwork_url, quote=True))
    result = result.replace("{{year}}", str(datetime.now().year))
    result = result.replace("{{link_blocks}}", "\n\n".join(link_blocks))
    result = result.replace("{{spotify_embed}}", spotify_embed)

    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(result)
    print(f"Generated {output_path}")


def main():
    parser = argparse.ArgumentParser(description="Generate links page from config")
    parser.add_argument(
        "-c", "--config",
        default=str(Path(__file__).resolve().parent.parent / "config" / "config.toml"),
        help="Path to config.toml",
    )
    parser.add_argument(
        "-t", "--template",
        default=str(Path(__file__).resolve().parent / "template.html"),
        help="Path to HTML template",
    )
    parser.add_argument(
        "-o", "--output",
        default=str(Path(__file__).resolve().parent / "index.html"),
        help="Output path for generated HTML",
    )
    args = parser.parse_args()
    generate(args.config, args.template, args.output)


if __name__ == "__main__":
    main()

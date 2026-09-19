#!/usr/bin/env python3
"""
Sync skills/packages from openclaw/clawhub to README.
"""

import sys
import json
import subprocess
import re
import urllib.request
import os

CLAWHUB_REPO = "openclaw/clawhub"


def get_latest_commit(url):
    """Get latest commit hash using git ls-remote."""
    try:
        result = subprocess.run(
            ["git", "ls-remote", url, "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return result.stdout.split()[0]
    except Exception as e:
        print(f"Error fetching git info: {e}", file=sys.stderr)
        return "unknown"


def fetch_url(url):
    """Fetch URL content with error handling."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OpenCode-Sync"})
        with urllib.request.urlopen(req, timeout=10) as response:
            return response.read().decode("utf-8")
    except Exception as e:
        print(f"Failed to fetch {url}: {e}", file=sys.stderr)
        return None


def get_clawhub_info():
    """Fetch latest info from clawhub repo."""
    base_url = f"https://api.github.com/repos/{CLAWHUB_REPO}"

    # Get latest commit
    latest_hash = get_latest_commit(f"https://github.com/{CLAWHUB_REPO}.git")

    # Fetch packages
    packages_url = f"{base_url}/contents/packages"
    packages_content = fetch_url(packages_url)
    packages = []
    if packages_content:
        try:
            data = json.loads(packages_content)
            packages = [item["name"] for item in data if item.get("type") == "dir"]
        except:
            pass

    # Fetch main README for description/CLI commands
    readme_url = f"https://raw.githubusercontent.com/{CLAWHUB_REPO}/main/README.md"
    readme_content = fetch_url(readme_url) or ""

    return {
        "latest_hash": latest_hash,
        "packages": packages,
        "readme": readme_content[:5000],
    }


def find_existing_section(content, marker):
    """Find existing section in README by marker."""
    pattern = rf"<!-- {marker} -->(.*?)<!-- /{marker} -->"
    match = re.search(pattern, content, re.DOTALL)
    return match.group(1) if match else None


def generate_packages_section(packages):
    """Generate packages section markdown."""
    if not packages:
        return "No packages found."

    lines = ["| Package | Description |", "| ------- | ----------- |"]
    desc_map = {
        "clawhub": "CLI tool for managing skills",
        "schema": "Shared API types and routes",
    }
    for pkg in packages:
        desc = desc_map.get(pkg, f"{pkg} package")
        lines.append(f"| `{pkg}` | {desc} |")

    return "\n".join(lines)


def generate_cli_section(readme):
    """Generate CLI commands section from README."""
    cli_patterns = [
        r"###?\s*(CLI|Commands|CLI Flows)",
        r"##?\s*(Common CLI)",
    ]

    # Extract CLI section if exists
    for pattern in cli_patterns:
        match = re.search(pattern, readme, re.IGNORECASE)
        if match:
            start = match.start()
            # Find next ## or end
            next_heading = re.search(r"\n##? ", readme[start + 1 :])
            if next_heading:
                cli_section = readme[start : start + next_heading.start()]
            else:
                cli_section = readme[start : start + 1000]
            return cli_section[:500]

    # Default CLI section
    default_cmds = """
| Command | Description |
| ------- | ----------- |
| `claw login` | Authenticate with GitHub |
| `claw search <query>` | Search skills |
| `claw explore` | Browse skill registry |
| `claw install <slug>` | Install a skill |
| `claw list` | List installed skills |
| `claw publish <path>` | Publish a new skill |
"""
    return default_cmds


def update_readme(info):
    """Update README with clawhub info."""
    # README is in repo root, not scripts directory
    readme_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "README.md")

    if not os.path.exists(readme_path):
        print(f"README.md not found at {readme_path}")
        return False

    with open(readme_path, "r", encoding="utf-8") as f:
        content = f.read()

    # Generate sections
    packages_section = generate_packages_section(info["packages"])
    cli_section = generate_cli_section(info["readme"])

    # Build new content
    new_sections = f"""
<!-- clawhub-sync-start -->
## ClawHub (Skill Registry)

Latest commit: `{info["latest_hash"][:7]}`

### Packages

{packages_section}

### CLI Commands

{cli_section}
<!-- clawhub-sync-end -->
"""

    # Replace or append
    pattern = r"<!-- clawhub-sync-start -->.*?<!-- clawhub-sync-end -->"
    if re.search(pattern, content, re.DOTALL):
        content = re.sub(pattern, new_sections.strip(), content, flags=re.DOTALL)
    else:
        content += "\n" + new_sections

    with open(readme_path, "w", encoding="utf-8") as f:
        f.write(content)

    print(f"Updated README with {len(info['packages'])} packages")
    return True


def main():
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"

    print(f"Fetching info from {CLAWHUB_REPO}...")
    info = get_clawhub_info()

    print(f"Latest commit: {info['latest_hash'][:7]}")
    print(f"Packages: {', '.join(info['packages'])}")

    if dry_run:
        print("DRY RUN - no changes made")
        return

    if update_readme(info):
        print("README.md updated successfully")
    else:
        print("Failed to update README.md")
        sys.exit(1)


if __name__ == "__main__":
    main()

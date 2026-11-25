import hashlib
import json
import os
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


def run(*args):
    subprocess.run(args, check=True)


def brew_info(formula):
    return json.loads(subprocess.check_output(["brew", "info", "--json=v2", formula]))["formulae"][0]


def release_info(tag):
    result = subprocess.run(["gh", "release", "view", tag, "--json", "isDraft,assets"],
                            capture_output=True, text=True, check=False)
    if result.returncode and result.stderr.strip() == "release not found":
        return None
    if result.returncode:
        raise SystemExit(result.stderr)
    return json.loads(result.stdout)


def publish_assets(tag, root, staging, uploads, expected):
    release = release_info(tag)
    existing = {a["url"]: a["digest"] for a in release["assets"]} if release else {}
    for url, sha in expected.values():
        if url.rsplit("/", 1)[-1] not in uploads and existing.get(url) != f"sha256:{sha}":
            raise ValueError(f"Cannot retain missing or mismatched asset {url}")
    if release is None:
        run("gh", "release", "create", tag, "--title", tag, "--notes", "", *(staging / n for n in uploads))
    else:
        changed = [staging / n for n, sha in uploads.items() if existing.get(f"{root}/{n}") != f"sha256:{sha}"]
        if changed:
            run("gh", "release", "upload", tag, "--clobber", *changed)
        if release["isDraft"]:
            run("gh", "release", "edit", tag, "--draft=false")

    assets = {a["url"]: a["digest"] for a in release_info(tag)["assets"]}
    for url, sha in expected.values():
        if assets.get(url) != f"sha256:{sha}":
            raise ValueError(f"Missing release asset or incorrect checksum for {url}")
        run("curl", "--fail", "--head", "--location", "--retry", "5", "--retry-all-errors",
            "--retry-delay", "2", "--silent", "--show-error", "--output", os.devnull, url)


def publish(staging):
    name = os.environ["FORMULA"]
    formula = f"{os.environ['TAP_NAME']}/{name}"
    version = os.environ["RELEASE_VERSION"]
    tag = os.environ["RELEASE_TAG"]
    root = os.environ["ROOT_URL"]
    bottles = Path(os.environ["BOTTLES_DIR"])
    formula_path = Path("Formula", f"{name}.rb")

    # A queued dispatch can predate a previous run's bottle commit.
    run("git", "merge", "--ff-only", f"origin/{os.environ['GITHUB_REF_NAME']}")
    current = brew_info(formula)
    old = {}
    if current["versions"]["stable"] == version:
        old = current["bottle"].get("stable", {})
    else:
        run("brew", "bump-formula-pr", "--write-only", "--no-audit",
            f"--url={os.environ['RELEASE_URL']}", f"--sha256={os.environ['RELEASE_SHA256']}", formula)

    tags = {}
    expected = {}
    for platform, file in old.get("files", {}).items():
        # Homebrew info prefixes symbolic cellars with a colon. Bottle JSON uses the bare name.
        tags[platform] = {"cellar": file["cellar"].removeprefix(":"), "sha256": file["sha256"]}
        expected[platform] = (file["url"], file["sha256"])
    rebuild = old.get("rebuild", 0)
    uploads = {}
    built_platforms = set()
    for manifest in sorted(bottles.glob("*.bottle.json")):
        record = json.loads(manifest.read_text())[formula]
        bottle = record["bottle"]
        if (record["formula"]["pkg_version"], bottle["root_url"], bottle["rebuild"]) != (version, root, rebuild):
            raise ValueError(f"Unexpected version, root URL or rebuild in {manifest}")
        for platform, file in bottle["tags"].items():
            if platform in built_platforms:
                raise ValueError(f"Multiple bottles for {platform}")
            built_platforms.add(platform)
            remote, local = file["filename"], file["local_filename"]
            if Path(remote).name != remote or Path(local).name != local or remote in uploads:
                raise ValueError(f"Invalid or duplicate bottle filename in {manifest}")
            source = bottles / local
            with source.open("rb") as stream:
                if hashlib.file_digest(stream, "sha256").hexdigest() != file["sha256"]:
                    raise ValueError(f"Bottle checksum mismatch for {source}")
            (staging / remote).symlink_to(source)
            uploads[remote] = file["sha256"]
            tags[platform] = {"cellar": file.get("cellar") or bottle["cellar"], "sha256": file["sha256"]}
            expected[platform] = (f"{root}/{remote}", file["sha256"])
    if not uploads:
        raise ValueError("No bottles to publish")

    merged = staging / "merged.json"
    merged.write_text(json.dumps({formula: {
        "formula": {"path": str(Path(os.environ["TAP_DIR"]) / formula_path), "pkg_version": version},
        "bottle": {"root_url": root, "rebuild": rebuild, "tags": tags},
    }}))
    run("brew", "bottle", "--merge", "--write", "--no-commit", "--no-all-checks", merged)
    merged_formula = brew_info(formula)
    if merged_formula["versions"]["stable"] != version:
        raise ValueError(f"Merged formula is not version {version}")
    files = merged_formula["bottle"]["stable"]["files"]
    if {p: (f["url"], f["sha256"]) for p, f in files.items()} != expected:
        raise ValueError("Merged formula does not match the retained and newly built bottles")
    run("brew", "audit", "--skip-style", formula)

    publish_assets(tag, root, staging, uploads, expected)

    if subprocess.check_output(["git", "diff", "--name-only", "HEAD", "--", formula_path]):
        run("git", "commit", "-m", f"{name}: update {version} bottle.", "--", formula_path)


if __name__ == "__main__":
    with TemporaryDirectory() as directory:
        publish(Path(directory))

"""docker-update-policy.json: read, validate and atomically write the update policy.

Missing file: the code defaults (auto). A file that exists but cannot be read,
is not valid JSON, or has the wrong shape is a problem: the scan falls back to
announce (check only) and alerts the administrator once. A damaged policy is
never silently treated as auto.

The supported maintenance entry point is this module:

    python3 -m homelab.policy --show
    python3 -m homelab.policy --set-default announce
    python3 -m homelab.policy --add-database-name my-app-db
    python3 -m homelab.policy --replace ...

Writes go to a temporary file in the same directory, are fsync'ed, and replace
the old file atomically. Existing custom list entries are preserved unless a
remove option is used. Built-in protections live in the code and are never
removed by this file.
"""
import argparse
import contextlib
import json
import os
import sys
import tempfile

from homelab import paths

FILENAME = "docker-update-policy.json"
DEFAULT_DATABASE_NAMES = {"n8n-postgres", "ghost-db", "paperless-redis"}
DEFAULT_DATABASE_IMAGES = ("postgres", "postgresql", "mysql", "mariadb", "mongo",
                           "mongodb", "redis", "valkey", "clickhouse", "influxdb", "couchdb")
DEFAULT_WATCHTOWER_MANAGED = {"daidai-panel"}
LIST_FIELDS = ("database_names", "database_images", "watchtower_managed")
DEFAULT_MODES = ("auto", "announce")


class Problem(ValueError):
    """The policy file exists but cannot be trusted."""


def path():
    return paths.data_file(FILENAME)


def _read_text(target):
    """Separate for tests: permission/IO failures must become a Problem."""
    return target.read_text()


def normalize(stored):
    """Validate a parsed policy object and return its settings.

    Unknown keys are ignored for forward compatibility. Known keys must have
    the documented types; a typo must not silently turn the default back to
    auto.
    """
    if not isinstance(stored, dict):
        raise Problem("顶层必须是 JSON 对象")
    mode = "auto"
    if "default" in stored:
        value = stored["default"]
        if not isinstance(value, str):
            raise Problem("default 必须是字符串")
        value = value.strip().lower()
        if value not in DEFAULT_MODES:
            raise Problem("default 只能是 auto 或 announce")
        mode = value
    lists = {}
    for key in LIST_FIELDS:
        if key not in stored:
            lists[key] = []
            continue
        value = stored[key]
        if not isinstance(value, list):
            raise Problem(f"{key} 必须是数组")
        items = []
        for item in value:
            if not isinstance(item, str) or not item.strip():
                raise Problem(f"{key} 只能包含非空字符串")
            item = item.strip()
            if item not in items:
                items.append(item)
        lists[key] = items
    return {"default": mode, **lists}


def read_raw():
    """Return (stored object or None, problem or None).

    (None, None) means the file does not exist. A dangling symlink is a
    problem, not a missing file: the operator expected a policy there.
    """
    target = path()
    try:
        text = _read_text(target)
    except FileNotFoundError:
        if os.path.lexists(target):
            return None, "策略文件是失效的符号链接"
        return None, None
    except UnicodeDecodeError:
        return None, "策略文件不是有效 UTF-8 文本"
    except OSError as ex:
        return None, f"策略文件无法读取（{type(ex).__name__}）"
    try:
        stored = json.loads(text)
    except (ValueError, RecursionError):
        return None, "策略文件不是有效 JSON"
    try:
        normalize(stored)
    except Problem as ex:
        return stored if isinstance(stored, dict) else None, str(ex)
    return stored, None


def load():
    """Effective policy, with the source and any degradation reason.

    Missing file -> defaults (auto). Damaged file -> default announce and a
    problem message; the caller must alert and must not auto-update.
    """
    result = {"default": "auto", "database_names": set(DEFAULT_DATABASE_NAMES),
              "database_images": set(DEFAULT_DATABASE_IMAGES),
              "watchtower_managed": set(DEFAULT_WATCHTOWER_MANAGED), "source": "missing"}
    stored, problem = read_raw()
    if problem:
        result.update(default="announce", problem=problem, source="degraded")
        return result
    if stored is None:
        return result
    try:
        settings = normalize(stored)
    except Problem as ex:
        result.update(default="announce", problem=str(ex), source="degraded")
        return result
    result["default"] = settings["default"]
    for key in LIST_FIELDS:
        result[key] |= set(settings[key])
    result["source"] = "file"
    return result


def effective_json(settings):
    return {"default": settings["default"],
            "database_names": sorted(settings["database_names"]),
            "database_images": sorted(settings["database_images"]),
            "watchtower_managed": sorted(settings["watchtower_managed"]),
            "source": settings.get("source", "unknown"),
            "problem": settings.get("problem")}


def save(stored, replace=False):
    """Validate and atomically replace the policy file.

    A damaged existing file is not overwritten unless replace=True; the
    caller has to acknowledge that the old contents cannot be trusted.
    """
    if not isinstance(stored, dict):
        raise Problem("顶层必须是 JSON 对象")
    clean = normalize(stored)
    target = path()
    if os.path.lexists(target) and not replace:
        _, problem = read_raw()
        if problem:
            raise Problem("现有策略文件异常，需 --replace 才能覆盖：" + problem)
    data = dict(stored)
    if "default" in data:
        data["default"] = clean["default"]
    for key in LIST_FIELDS:
        if key in data:
            data[key] = clean[key]
    target.parent.mkdir(parents=True, exist_ok=True)
    directory = str(target.parent)
    fd, temp = tempfile.mkstemp(prefix="." + target.name + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temp, 0o600)
        os.replace(temp, target)
        dir_fd = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(temp)
        raise


def _stored_for_update(replace):
    stored, problem = read_raw()
    if problem:
        if not replace:
            raise Problem("现有策略文件异常，需 --replace 才能覆盖：" + problem)
        return {}
    return dict(stored or {})


def _add(stored, key, values):
    current = stored.get(key, [])
    if not isinstance(current, list):
        raise Problem(f"{key} 必须是数组")
    for value in values:
        value = value.strip()
        if not value:
            raise Problem(f"{key} 不能加入空值")
        if value not in current:
            current.append(value)
    stored[key] = current


def _remove(stored, key, values):
    current = stored.get(key, [])
    if not isinstance(current, list):
        raise Problem(f"{key} 必须是数组")
    for value in values:
        value = value.strip()
        if value not in current:
            raise Problem(f"{key} 中没有自定义项 {value!r}（内置保护不能在这里删除）")
        current.remove(value)
    stored[key] = current


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python3 -m homelab.policy",
        description="Show or atomically edit docker-update-policy.json.")
    parser.add_argument("--show", action="store_true", help="print the effective policy and exit")
    parser.add_argument("--set-default", choices=DEFAULT_MODES)
    parser.add_argument("--add-database-name", action="append", default=[], metavar="NAME")
    parser.add_argument("--add-database-image", action="append", default=[], metavar="IMAGE")
    parser.add_argument("--add-watchtower", action="append", default=[], metavar="NAME")
    parser.add_argument("--remove-database-name", action="append", default=[], metavar="NAME")
    parser.add_argument("--remove-database-image", action="append", default=[], metavar="IMAGE")
    parser.add_argument("--remove-watchtower", action="append", default=[], metavar="NAME")
    parser.add_argument("--replace", action="store_true",
                        help="overwrite an unreadable or invalid existing policy file")
    args = parser.parse_args(argv)
    changes = any((args.set_default, args.add_database_name, args.add_database_image,
                   args.add_watchtower, args.remove_database_name, args.remove_database_image,
                   args.remove_watchtower))
    if not changes:
        if args.show:
            settings = load()
            print(json.dumps(effective_json(settings), ensure_ascii=False, indent=2, sort_keys=True))
            return 1 if settings.get("problem") else 0
        parser.print_help()
        return 2
    try:
        stored = _stored_for_update(args.replace)
        if args.set_default:
            stored["default"] = args.set_default
        _add(stored, "database_names", args.add_database_name)
        _add(stored, "database_images", args.add_database_image)
        _add(stored, "watchtower_managed", args.add_watchtower)
        _remove(stored, "database_names", args.remove_database_name)
        _remove(stored, "database_images", args.remove_database_image)
        _remove(stored, "watchtower_managed", args.remove_watchtower)
        save(stored, replace=args.replace)
    except (Problem, OSError) as ex:
        print("error: " + str(ex), file=sys.stderr)
        return 2
    settings = load()
    print(json.dumps(effective_json(settings), ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())

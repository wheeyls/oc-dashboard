"""oc-kanban CLI — thin shell interface over the Kanban adapter.

Designed to be called by OpenCode agents via bash.  Every subcommand
prints structured, grep-friendly output so agents can parse results.

Usage:
    oc-kanban list [--stage STAGE]
    oc-kanban show <id>
    oc-kanban create <title> [--desc DESC] [--stage STAGE] [--tag TAG ...]
    oc-kanban move <id> <stage>
    oc-kanban update <id> [--title TITLE] [--desc DESC] [--stage STAGE]
    oc-kanban delete <id>
    oc-kanban link-session <id> <session_id>
    oc-kanban unlink-session <id> <session_id>
    oc-kanban link-pr <id> <pr_number>
    oc-kanban unlink-pr <id> <pr_number>
    oc-kanban stages
"""

import argparse
import sys

from .kanban import STAGES, STAGE_LABELS, LocalJsonKanban


def _adapter():
    # type: () -> LocalJsonKanban
    return LocalJsonKanban()


def _print_project(p, verbose=False):
    """Print a single project in agent-friendly format."""
    print("id=%s  stage=%s  title=%s" % (p.id, p.stage, p.title))
    if verbose:
        if p.description:
            print("  desc=%s" % p.description)
        if p.session_ids:
            print("  sessions=%s" % ",".join(p.session_ids))
        if p.pr_numbers:
            print("  prs=%s" % ",".join(str(n) for n in p.pr_numbers))
        if p.tags:
            print("  tags=%s" % ",".join(p.tags))
        print("  created=%s  updated=%s" % (p.created_at, p.updated_at))


def cmd_list(args):
    adapter = _adapter()
    projects = adapter.list_projects()
    if args.stage:
        projects = [p for p in projects if p.stage == args.stage]
    if not projects:
        print("No projects found.")
        return
    for p in projects:
        _print_project(p)


def cmd_show(args):
    adapter = _adapter()
    p = adapter.get_project(args.id)
    if not p:
        print("Project not found: %s" % args.id)
        sys.exit(1)
    _print_project(p, verbose=True)


def cmd_create(args):
    adapter = _adapter()
    stage = args.stage if args.stage and args.stage in STAGES else "pending"
    tags = args.tag if args.tag else []
    p = adapter.create_project(
        title=args.title,
        description=args.desc or "",
        stage=stage,
        tags=tags,
    )
    print("Created project:")
    _print_project(p, verbose=True)


def cmd_move(args):
    if args.stage not in STAGES:
        print("Invalid stage: %s. Valid: %s" % (args.stage, ", ".join(STAGES)))
        sys.exit(1)
    adapter = _adapter()
    p = adapter.move_project(args.id, args.stage)
    if not p:
        print("Project not found: %s" % args.id)
        sys.exit(1)
    print("Moved to %s:" % args.stage)
    _print_project(p)


def cmd_update(args):
    adapter = _adapter()
    kwargs = {}
    if args.title:
        kwargs["title"] = args.title
    if args.desc is not None:
        kwargs["description"] = args.desc
    if args.stage:
        kwargs["stage"] = args.stage
    if not kwargs:
        print("Nothing to update. Use --title, --desc, or --stage.")
        sys.exit(1)
    p = adapter.update_project(args.id, **kwargs)
    if not p:
        print("Project not found: %s" % args.id)
        sys.exit(1)
    print("Updated:")
    _print_project(p, verbose=True)


def cmd_delete(args):
    adapter = _adapter()
    ok = adapter.delete_project(args.id)
    if ok:
        print("Deleted project: %s" % args.id)
    else:
        print("Project not found: %s" % args.id)
        sys.exit(1)


def cmd_link_session(args):
    adapter = _adapter()
    ok = adapter.link_session(args.id, args.session_id)
    if ok:
        print("Linked session %s to project %s" % (args.session_id, args.id))
    else:
        print("Project not found: %s" % args.id)
        sys.exit(1)


def cmd_unlink_session(args):
    adapter = _adapter()
    ok = adapter.unlink_session(args.id, args.session_id)
    if ok:
        print("Unlinked session %s from project %s" % (args.session_id, args.id))
    else:
        print("Project not found: %s" % args.id)
        sys.exit(1)


def cmd_link_pr(args):
    adapter = _adapter()
    ok = adapter.link_pr(args.id, args.pr_number)
    if ok:
        print("Linked PR #%d to project %s" % (args.pr_number, args.id))
    else:
        print("Project not found: %s" % args.id)
        sys.exit(1)


def cmd_unlink_pr(args):
    adapter = _adapter()
    ok = adapter.unlink_pr(args.id, args.pr_number)
    if ok:
        print("Unlinked PR #%d from project %s" % (args.pr_number, args.id))
    else:
        print("Project not found: %s" % args.id)
        sys.exit(1)


def cmd_stages(_args):
    for s in STAGES:
        print("%s  %s" % (s, STAGE_LABELS[s]))


def main():
    parser = argparse.ArgumentParser(
        prog="oc-kanban",
        description="Kanban board CLI for oc-dashboard",
    )
    sub = parser.add_subparsers(dest="command")

    # list
    p_list = sub.add_parser("list", help="List projects")
    p_list.add_argument("--stage", choices=STAGES, help="Filter by stage")

    # show
    p_show = sub.add_parser("show", help="Show project details")
    p_show.add_argument("id", help="Project ID")

    # create
    p_create = sub.add_parser("create", help="Create a project")
    p_create.add_argument("title", help="Project title")
    p_create.add_argument("--desc", default="", help="Description")
    p_create.add_argument("--stage", choices=STAGES, default="pending")
    p_create.add_argument("--tag", action="append", help="Tag (repeatable)")

    # move
    p_move = sub.add_parser("move", help="Move project to a stage")
    p_move.add_argument("id", help="Project ID")
    p_move.add_argument("stage", choices=STAGES, help="Target stage")

    # update
    p_update = sub.add_parser("update", help="Update project fields")
    p_update.add_argument("id", help="Project ID")
    p_update.add_argument("--title", help="New title")
    p_update.add_argument("--desc", help="New description")
    p_update.add_argument("--stage", choices=STAGES, help="New stage")

    # delete
    p_delete = sub.add_parser("delete", help="Delete a project")
    p_delete.add_argument("id", help="Project ID")

    # link-session
    p_ls = sub.add_parser("link-session", help="Link session to project")
    p_ls.add_argument("id", help="Project ID")
    p_ls.add_argument("session_id", help="OpenCode session ID")

    # unlink-session
    p_us = sub.add_parser("unlink-session", help="Unlink session from project")
    p_us.add_argument("id", help="Project ID")
    p_us.add_argument("session_id", help="OpenCode session ID")

    # link-pr
    p_lp = sub.add_parser("link-pr", help="Link PR to project")
    p_lp.add_argument("id", help="Project ID")
    p_lp.add_argument("pr_number", type=int, help="PR number")

    # unlink-pr
    p_up = sub.add_parser("unlink-pr", help="Unlink PR from project")
    p_up.add_argument("id", help="Project ID")
    p_up.add_argument("pr_number", type=int, help="PR number")

    # stages
    sub.add_parser("stages", help="List valid stages")

    args = parser.parse_args()

    dispatch = {
        "list": cmd_list,
        "show": cmd_show,
        "create": cmd_create,
        "move": cmd_move,
        "update": cmd_update,
        "delete": cmd_delete,
        "link-session": cmd_link_session,
        "unlink-session": cmd_unlink_session,
        "link-pr": cmd_link_pr,
        "unlink-pr": cmd_unlink_pr,
        "stages": cmd_stages,
    }

    if args.command and args.command in dispatch:
        dispatch[args.command](args)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()

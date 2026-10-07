import argparse
from .queue import init_db, add_task, all_tasks
from .runner import run_forever


def main():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_add = sub.add_parser("add")
    p_add.add_argument("description")
    p_add.add_argument("--name", required=True)
    p_add.add_argument("--priority", type=int, default=0)

    sub.add_parser("list")
    sub.add_parser("run")

    args = parser.parse_args()
    init_db()

    if args.cmd == "add":
        tid = add_task(args.name, args.description, args.priority)
        print(f"任务已加入队列: {tid}")

    elif args.cmd == "list":
        for t in all_tasks():
            print(f"{t['id']}  {t['status']:8}  {t['name']}")

    elif args.cmd == "run":
        run_forever()


if __name__ == "__main__":
    main()
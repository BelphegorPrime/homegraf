"""Menu "Manage secrets": Komodo server -> stack -> add, change, show, delete keys, deploy."""
from __future__ import annotations

import pathlib
import sys

CODE = pathlib.Path(__file__).resolve().parents[2]    # where the tools are
sys.path.insert(0, str(CODE / "infra/infisical"))
import secret  # noqa: E402
import ui  # noqa: E402

COMMANDS = ("[n]ew  [c]hange <no>  [s]how <no>  [d]elete <no>  [r]edeploy  [b]ack  [q]uit\n"
            "  e.g. 'n' adds a key, 'c 2' changes key 2, 's 1' shows key 1's value: ")


def stack_menu(inf, stack: dict) -> bool:
    """Menu for one stack. False = quit the program."""
    folder, changed = secret.Folder(inf, stack), False
    while True:
        keys = folder.keys()
        print(f"\n== {stack['id']}  (Infisical {folder.where}, Komodo stack {stack['komodo_stack']})")
        for i, k in enumerate(keys, 1):
            print(f"  {i:2}) {k}" + ("" if secret.compose_refs(stack, k) else secret.UNREFERENCED))
        if not keys:
            print("  (no secrets yet)")
        cmd, _, arg = input(COMMANDS).strip().partition(" ")
        cmd, arg = cmd.lower(), arg.strip()
        key = keys[int(arg) - 1] if arg.isdigit() and 1 <= int(arg) <= len(keys) else None
        try:
            if cmd in ("c", "s", "d") and key is None:
                print(f"  add the number of the key, e.g. '{cmd} 1'")
            elif cmd in ("n", "c"):
                if cmd == "n":
                    key = secret.check_key(ui.ask("Name", example="DB_PASSWORD"))
                    if key in keys:
                        print(f"  {key} already exists; change it with 'c <no>'")
                        continue
                value, generated = ui.secret_value(key)
                print(f"  {folder.where}/{key} {folder.set(key, value)}")
                changed = True
                if generated and ui.yes("  Show the generated value?"):
                    print(f"  {value}")
                if not secret.compose_refs(stack, key):
                    print(f"  Note: no compose file of {stack['id']} uses ${{{key}}} yet;"
                          f" reference it there, e.g. `{key}: ${{{key}}}` under environment:")
            elif cmd == "s":
                print(f"  {key} = {folder.get(key)}")
            elif cmd == "d":
                if secret.compose_refs(stack, key):
                    print(f"  Warning: {stack['id']} still uses ${{{key}}}; without a value the next deploy fails"
                          " or the service gets an empty value.")
                if ui.yes(f"  Delete {folder.where}/{key}?"):
                    folder.delete(key)
                    print("  deleted")
                    changed = True
            elif cmd == "r":
                secret.deploy(stack)
                changed = False
            elif cmd in ("b", "q"):
                if changed and ui.yes(f"Changes take effect on the next deploy. Redeploy {stack['komodo_stack']} now?"):
                    secret.deploy(stack)
                return cmd == "b"
            else:
                print("  unknown command")
        except SystemExit as e:  # a failed step does not end the session
            print(f"  Error: {e}")


def run(infisical) -> bool:
    """Server and stack choice; infisical() returns the API client. False = quit the program."""
    stacks = secret.secret_stacks()
    servers = sorted({s["server"] for s in stacks})
    while True:
        i = ui.choose("Which Komodo server? (only stacks whose secrets are in Infisical are listed)",
                      [f"{srv}  ({sum(s['server'] == srv for s in stacks)} stacks)" for srv in servers])
        if i is None:
            return True
        on_server = sorted((s for s in stacks if s["server"] == servers[i]), key=lambda s: s["id"])
        j = ui.choose(f"Which stack on {servers[i]}?", [s["id"] for s in on_server])
        if j is not None and not stack_menu(infisical(), on_server[j]):
            return False

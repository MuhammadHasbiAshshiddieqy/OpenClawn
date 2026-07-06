"""Terapkan clawn.yaml ke soul.toml tiap role (TODO.md § Prioritas 3).

Manifest deklaratif tim/role DI ATAS soul.toml — operator menulis policy
sekali per tool di clawn.yaml, skrip ini menyisipkan/mengganti blok
`[policy.<tool>]` di soul.toml role terkait. Section lain soul.toml
(system_prompt, tools, routing, contract) TIDAK disentuh.

Pakai:
    python scripts/apply_manifest.py                        # clawn.yaml di root, roles/ default
    python scripts/apply_manifest.py --manifest custom.yaml
    python scripts/apply_manifest.py --roles-dir roles

Contoh clawn.yaml:
    team:
      pm:
        policy:
          pdf_write:
            approval_required_if:
              - field: content
                op: contains
                value: confidential
      dev:
        policy:
          shell_run:
            approval_required_if:
              - op: always
"""

import argparse
import sys
from pathlib import Path

# scripts/ tidak masuk package (lihat pyproject packages.find) → tambah root proyek
# ke path agar import absolut (infra.*) bekerja saat dijalankan dari mana pun.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from infra.manifest import ManifestError, apply_manifest  # noqa: E402

DEFAULT_MANIFEST = "clawn.yaml"
DEFAULT_ROLES_DIR = "roles"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--manifest",
        default=DEFAULT_MANIFEST,
        help=f"Path clawn.yaml (default: {DEFAULT_MANIFEST})",
    )
    parser.add_argument(
        "--roles-dir",
        default=DEFAULT_ROLES_DIR,
        help=f"Direktori roles/ (default: {DEFAULT_ROLES_DIR})",
    )
    args = parser.parse_args()

    try:
        updated = apply_manifest(args.manifest, roles_dir=args.roles_dir)
    except ManifestError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)

    if not updated:
        print("Tidak ada role yang diproses (tidak ada key 'policy' di manifest).")
        return

    print(f"soul.toml diperbarui untuk {len(updated)} role:")
    for role in updated:
        print(f"  - {role}")


if __name__ == "__main__":
    main()

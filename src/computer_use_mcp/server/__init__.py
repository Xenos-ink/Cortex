"""Console-script launch shim (D1)."""

def main():
    from .app import main as _main
    return _main()

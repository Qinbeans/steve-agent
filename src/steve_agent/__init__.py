__all__ = ["main"]


def main():
    # Imported here so that importing the package does not load deepagents.
    from steve_agent.__main__ import main as cli_main

    return cli_main()

"""Start the research department on this machine.

    python -m research_department
"""

from research_department.http_server import DepartmentServer
from research_department.service import ResearchDesk


def main() -> None:
    import os
    import threading
    from pathlib import Path

    from research_department.mail import MailService
    from research_department.phone import ensure, phone_url

    port = int(os.environ.get("RESEARCH_PORT", "8790"))
    library = Path(__file__).resolve().parents[1] / "data" / "strategies.json"
    desk = ResearchDesk(library_path=library)

    def application_url() -> str:
        return phone_url() or desk.url

    desk.mail = MailService(phone=application_url)
    server = DepartmentServer(desk, port=port)
    if desk.has_backtest():
        desk.start_worker()
    threading.Thread(target=ensure, args=(port,), name="research-phone", daemon=True).start()
    print(f"Research department {server.url}", flush=True)
    try:
        server.thread.join()
    except KeyboardInterrupt:
        server.close()


if __name__ == "__main__":
    main()

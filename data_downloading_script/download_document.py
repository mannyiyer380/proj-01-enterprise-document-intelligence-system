"""Discover and download files from the publishers' official document pages."""

import re
from collections.abc import Iterator
from dataclasses import dataclass
from html import unescape
from html.parser import HTMLParser
from pathlib import Path
from tempfile import NamedTemporaryFile
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

BASE_DIR = Path(__file__).resolve().parent / "downloads"
DEFAULT_COUNT = 15
MIB = 1024 * 1024
MAX_DOCUMENT_BYTES = 128 * MIB
REQUEST_HEADERS = {
    "User-Agent": "PublicDocumentDownloader/1.0",
    "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.1",
    "Accept-Encoding": "identity",
}

PUBLISHER_PAGES = {
    "RBI": "https://www.rbi.org.in/Scripts/BS_ViewMasterDirections.aspx?did=403",
    "IRDAI": "https://irdai.gov.in/circulars",
    "HDFC Life": "https://www.hdfclife.com/about-us/investor-relations#:~:text=Annual%20Reports",
    "LIC": "https://web.licindia.in/web/guest/annual-reports1",
}


@dataclass(frozen=True)
class Document:
    publisher: str
    title: str
    filename: str
    url: str
    html_marker: str = ""

    @property
    def file_type(self) -> str:
        return "HTML" if self.html_marker else "PDF"


@dataclass(frozen=True)
class DownloadResult:
    status: str
    detail: str = ""


class PublisherLinks(HTMLParser):
    def __init__(self, html: str):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self._link = ""
        self._label: list[str] = []
        self.feed(html)

    def handle_starttag(self, tag, attrs):
        if tag == "a":
            self._link = dict(attrs).get("href") or ""
            self._label = []

    def handle_data(self, data):
        if self._link:
            self._label.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._link:
            self.links.append((self._link, " ".join("".join(self._label).split())))
            self._link = ""


def filename_slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "_", value).strip("_")[:112].rstrip("_")


def document_from_link(publisher: str, label: str, url: str) -> Document | None:
    parsed = urlsplit(url)
    path = parsed.path.lower()

    if publisher == "RBI":
        direction_id = parse_qs(parsed.query).get("id", [""])[0]
        if not path.endswith("/bs_viewmasdirections.aspx") or not direction_id.isdecimal() or not label:
            return None
        title = re.sub(r"^Reserve Bank of India\s*", "", label).strip()
        title = re.sub(r"\s*\(Updated as on .*\)$", "", title, flags=re.I)
        subject = re.search(r"Commercial Banks\s*[-–]\s*(.*?)(?:\)\s*Directions|]\s*Directions)", title, re.I)
        marker = subject.group(1) if subject else title
        marker = re.split(r"[:–]", marker, maxsplit=1)[0]
        return Document(publisher, title, f"RBI_{direction_id}_{filename_slug(title)}.html", url, marker)

    if publisher == "IRDAI":
        if not path.startswith("/documents/") or ".pdf" not in path or not label.lower().endswith(".pdf"):
            return None
        title = re.sub(r"^[^A-Za-z]+", "", label.rsplit("_", 1)[-1][:-4]).strip()
        if not title:
            return None
        return Document(publisher, title, f"IRDAI_{filename_slug(title)}.pdf", url)

    fiscal = re.search(r"\b(20\d{2})\s*[-–]\s*(?:20)?(\d{2})\b", label)
    if not fiscal:
        return None
    if publisher == "HDFC Life":
        if "/annual-reports/" not in path or not path.endswith(".pdf"):
            return None
        if not re.search(r"annual[\s-]+report", label, re.I):
            return None
    elif publisher == "LIC":
        if not path.startswith("/documents/") or not re.match(r"FY\s+", label, re.I):
            return None

    year, end = fiscal.groups()
    title = f"Annual Report {year}-{end}"
    return Document(publisher, title, f"{filename_slug(publisher)}_Annual_Report_{year}_{end}.pdf", url)


def irdai_page_number(url: str) -> int:
    query = parse_qs(urlsplit(url).query)
    for key, values in query.items():
        if key.endswith("IRDAIDocumentMediaPortlet_cur") and values[0].isdecimal():
            return int(values[0])
    return 1


def discover_documents(publisher: str, client: requests.Session) -> Iterator[Document]:
    """Read the publisher's live listing in the order it publishes the links."""
    page = PUBLISHER_PAGES[publisher]
    site = urlsplit(page)
    domain = site.hostname.removeprefix("www.").removeprefix("web.")
    seen_pages = set()
    seen_documents = set()
    seen_filenames = set()

    while page and page not in seen_pages:
        seen_pages.add(page)
        with client.get(page, headers=REQUEST_HEADERS, timeout=(10, 30)) as response:
            response.raise_for_status()
            links = PublisherLinks(response.text).links

        next_pages = {}
        current_number = irdai_page_number(page)
        for href, label in links:
            url = urljoin(page, href).split("#", 1)[0]
            parsed = urlsplit(url)
            host = parsed.hostname or ""
            if parsed.scheme != "https" or (host != domain and not host.endswith("." + domain)):
                continue
            if publisher == "IRDAI" and parsed.path == site.path:
                number = irdai_page_number(url)
                if number > current_number:
                    next_pages[number] = url
            if url in seen_documents:
                continue
            document = document_from_link(publisher, label, url)
            if document:
                seen_documents.add(url)
                if document.filename not in seen_filenames:
                    seen_filenames.add(document.filename)
                    yield document

        page = next_pages[min(next_pages)] if next_pages else ""


def normalized_html_text(value: str) -> str:
    text = re.sub(r"<[^>]*>", " ", unescape(value)).casefold()
    return " ".join(re.findall(r"[a-z0-9]+", text))


def matches_document(path: Path, document: Document, size: int) -> bool:
    with path.open("rb") as file:
        if document.html_marker:
            head = file.read(256 * 1024).decode("utf-8", errors="ignore")
            return "<html" in head.lower() and normalized_html_text(document.html_marker) in normalized_html_text(head)
        if file.read(5) != b"%PDF-":
            return False
        file.seek(max(0, size - 4096))
        return b"%%EOF" in file.read()


def download_file(document: Document, client: requests.Session, directory: Path = BASE_DIR) -> DownloadResult:
    destination = directory / document.filename
    temporary = None
    try:
        if destination.is_file():
            size = destination.stat().st_size
            if matches_document(destination, document, size):
                return DownloadResult("Already saved")

        directory.mkdir(parents=True, exist_ok=True)
        with client.get(document.url, headers=REQUEST_HEADERS, timeout=(10, 45), stream=True) as response:
            response.raise_for_status()
            content_length = response.headers.get("Content-Length", "")
            unencoded = response.headers.get("Content-Encoding", "identity").lower() == "identity"
            expected_size = int(content_length) if unencoded and content_length.isdecimal() else None
            if expected_size is not None and expected_size > MAX_DOCUMENT_BYTES:
                raise ValueError(f"File exceeds the {MAX_DOCUMENT_BYTES // MIB} MiB download limit.")

            size = 0
            with NamedTemporaryFile(dir=directory, suffix=".part", delete=False) as file:
                temporary = Path(file.name)
                for chunk in response.iter_content(MIB):
                    size += len(chunk)
                    if size > MAX_DOCUMENT_BYTES:
                        raise ValueError(f"File exceeds the {MAX_DOCUMENT_BYTES // MIB} MiB download limit.")
                    file.write(chunk)

        if expected_size is not None and size != expected_size:
            raise ValueError("The server ended the download before sending the full file.")
        if not matches_document(temporary, document, size):
            raise ValueError(f"The source returned an unexpected or incomplete {document.file_type} document.")

        temporary.replace(destination)
        temporary = None
        return DownloadResult("Downloaded")
    except requests.HTTPError as error:
        detail = f"The publisher returned HTTP {error.response.status_code}."
    except requests.Timeout:
        detail = "The publisher took too long to respond."
    except requests.RequestException:
        detail = "The publisher connection was interrupted."
    except (OSError, ValueError) as error:
        detail = str(error)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return DownloadResult("Failed", detail=detail)


def main() -> None:
    from itertools import islice

    with requests.Session() as client:
        for publisher in PUBLISHER_PAGES:
            try:
                for document in islice(discover_documents(publisher, client), DEFAULT_COUNT):
                    result = download_file(document, client)
                    print(f"{result.status}: {document.filename} {result.detail}".rstrip())
            except requests.RequestException:
                print(f"Could not open the document page for {publisher}.")


if __name__ == "__main__":
    main()

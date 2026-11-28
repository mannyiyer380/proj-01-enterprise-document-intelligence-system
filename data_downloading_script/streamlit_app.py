"""Standalone Streamlit interface for downloading from public publisher websites."""

import requests
import streamlit as st

from download_document import DEFAULT_COUNT, PUBLISHER_PAGES, discover_documents, download_file


def download_documents(publisher: str, count: int) -> None:
    progress = st.progress(0, text=f"Reading the {publisher} document page...")
    downloaded = already_saved = 0
    first_error = ""

    try:
        with requests.Session() as client:
            for document in discover_documents(publisher, client):
                progress.progress(
                    (downloaded + already_saved) / count,
                    text=f"Downloading from {publisher} · {downloaded + already_saved}/{count} ready",
                )
                result = download_file(document, client)
                if result.status == "Downloaded":
                    downloaded += 1
                elif result.status == "Already saved":
                    already_saved += 1
                elif not first_error:
                    first_error = result.detail
                if downloaded + already_saved == count:
                    break
    except requests.RequestException:
        first_error = "Could not read the official document page. Check the publisher link above."

    progress.empty()
    ready = downloaded + already_saved
    if ready:
        message = (
            f"{publisher} has {ready} of {count} files ready in `downloads/` "
            f"({downloaded} downloaded, {already_saved} already saved)."
        )
        if ready == count:
            st.success(message)
        else:
            st.warning(f"{message} {first_error or 'The publisher listing has no more matching files.'}")
    else:
        reason = first_error or "The official page did not list downloadable documents."
        st.error(f"{publisher} download failed. {reason}")


def main() -> None:
    st.set_page_config(page_title="Document Downloader")
    st.title("Document downloader")

    count = st.number_input("Documents per publisher", min_value=1, value=DEFAULT_COUNT, step=1)
    download_all = st.button("Download from all publishers", type="primary", key="download_all")
    publishers = list(PUBLISHER_PAGES.items())
    selected = []
    for start in range(0, len(publishers), 2):
        for column, (publisher, url) in zip(st.columns(2), publishers[start:start + 2]):
            with column.container(border=True):
                st.subheader(publisher)
                st.markdown(f"[Open the official {publisher} document page]({url})")
                if st.button(f"Download {publisher}", key=f"download_{publisher}"):
                    selected.append(publisher)

    for publisher in PUBLISHER_PAGES if download_all else selected:
        download_documents(publisher, count)


if __name__ == "__main__":
    main()

from pathlib import Path
import requests

BASE_DIR = Path("documents")

SOURCES = {
    "01_RBI": [
        {
            "name": "RBI_KYC_Directions_2025.html",
            "url": "https://rbi.org.in/Scripts/BS_ViewMasDirections.aspx?id=13141"
        }
    ],

    "02_IRDAI": [
        {
            "name": "IRDAI_Life_Insurance_Master_Circular_2024.pdf",
            "url": "https://irdai.gov.in/documents/37343/991022/%E0%A4%85%E0%A4%82%E0%A4%97%E0%A5%8D%E0%A4%B0%E0%A5%87%E0%A4%9C%E0%A5%80%2B-%2B%E0%A4%9C%E0%A5%80%E0%A4%B5%E0%A4%A8%2B%E0%A4%AC%E0%A5%80%E0%A4%AE%E0%A4%BE%2B%E0%A4%89%E0%A4%A4%E0%A5%8D%E0%A4%AA%E0%A4%BE%E0%A4%A6%E0%A5%8B%E0%A4%82%2B%E0%A4%B8%E0%A4%82%E0%A4%AC%E0%A4%82%E0%A4%A7%E0%A5%80%2B%E0%A4%AE%E0%A4%BE%E0%A4%B8%E0%A5%8D%E0%A4%9F%E0%A4%B0%2B%E0%A4%AA%E0%A4%B0%E0%A4%BF%E0%A4%AA%E0%A4%A4%E0%A5%8D%E0%A4%B0%2B_%2BMaster%2BCircular%2Bon%2BLife%2BInsurance%2BProducts%2B-%2BEnglish.pdf/56f5b93b-a7c6-30cf-1734-0537e2d8676d?download=true&t=1733233169949&version=1.0"
        }
    ],

    "03_HDFC_Life": [
        {
            "name": "HDFC_Life_Annual_Report_2024_25.pdf",
            "url": "https://www.hdfclife.com/content/dam/hdfclifeinsurancecompany/about-us/pdf/investor-relations/financial-information/annual-reports/integrated-annual-report-fy-2024-2025.pdf"
        }
    ],

    "04_LIC": [
        {
            "name": "LIC_Annual_Report_2024_25.pdf",
            "url": "https://licindia.in/documents/d/guest/annual-report-of-the-corporation-for-fy-2024-25"
        }
    ]
}


def download_file(url: str, output_path: Path):
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 Chrome/130 Safari/537.36"
        )
    }

    try:
        print(f"Downloading: {output_path.name}")

        response = requests.get(
            url,
            headers=headers,
            timeout=60,
            stream=True
        )

        response.raise_for_status()

        with open(output_path, "wb") as file:
            for chunk in response.iter_content(chunk_size=1024 * 1024):
                if chunk:
                    file.write(chunk)

        size_mb = output_path.stat().st_size / (1024 * 1024)

        print(f"Downloaded: {output_path}")
        print(f"Size: {size_mb:.2f} MB")
        print("-" * 60)

    except Exception as e:
        print(f"Failed: {url}")
        print(f"Error: {e}")
        print("-" * 60)


def main():

    BASE_DIR.mkdir(exist_ok=True)

    for folder_name, documents in SOURCES.items():

        folder_path = BASE_DIR / folder_name
        folder_path.mkdir(parents=True, exist_ok=True)

        for document in documents:

            output_path = folder_path / document["name"]

            if output_path.exists():
                print(f"Already exists: {output_path.name}")
                continue

            download_file(
                document["url"],
                output_path
            )


if __name__ == "__main__":
    main()
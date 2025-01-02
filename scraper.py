import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

def scrape_site(start_url):
    visited = set()
    content = {}

    def scrape_page(url):
        try:
            response = requests.get(url, timeout = 10) #TIMEOUT
            response.raise_for_status()
            soup = BeautifulSoup(response.text, "html.parser")
            text = soup.get_text(separator='')
            return text, [urljoin(url, a['href']) for a in soup.find_all('a', href=True)]
        except Exception as e:
            print(f"ERror scraping {url}: {e}")
            return None, []
        
    to_visit = [start_url]
    while to_visit:
        current_url = to_visit.pop(0)
        if current_url in visited:
            continue

        visited.add(current_url)
        page_content, links = scrape_page(current_url)
        if page_content:
            content[current_url] = page_content

        to_visit.extend([link for link in links if link not in visited])

    return content
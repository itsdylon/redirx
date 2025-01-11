import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from urllib.parse import urljoin, urlparse

def scrape_site(start_url):
    visited = set()
    content = {}
    pages_attempted = 0
    pages_successful = 0
    
    # Get the base domain of the starting URL
    base_domain = urlparse(start_url).netloc
    print(f"Starting scrape of domain: {base_domain}")

    session = requests.Session()
    session.headers.update({
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5',
    })

    def scrape_page(url):
        nonlocal pages_attempted, pages_successful
        pages_attempted += 1
        try:
            print(f"Attempting to scrape: {url}")
            response = session.get(url, timeout=10, verify=False)
            response.raise_for_status()
            
            # Check if we got HTML content
            content_type = response.headers.get('content-type', '')
            if 'text/html' not in content_type.lower():
                print(f"Skipping non-HTML content at {url} (Content-Type: {content_type})")
                return None, []

            soup = BeautifulSoup(response.text, "html.parser")
            text = soup.get_text(separator=' ').strip()
            
            # Only process if we got meaningful text
            if len(text) < 50:  # Arbitrary minimum length for a real page
                print(f"Skipping page with insufficient content at {url}")
                return None, []

            links = []
            for a in soup.find_all('a', href=True):
                full_url = urljoin(url, a['href'])
                parsed_url = urlparse(full_url)
                if parsed_url.netloc == base_domain:
                    links.append(full_url)
            
            pages_successful += 1
            print(f"Successfully scraped {url} (Found {len(links)} internal links)")
            return text, links

        except requests.exceptions.RequestException as e:
            print(f"Request failed for {url}: {str(e)}")
            return None, []
        except Exception as e:
            print(f"Error processing {url}: {str(e)}")
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

    print(f"\nScraping Summary:")
    print(f"Pages attempted: {pages_attempted}")
    print(f"Pages successful: {pages_successful}")
    print(f"Total unique URLs found: {len(visited)}")
    print(f"Pages with content: {len(content)}")

    return content
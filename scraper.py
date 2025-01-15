import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlparse
import time
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

class Scraper:
    def __init__(self):
        self.session = self._create_session()
        self.mobile_user_agent = 'Mozilla/5.0 (iPhone; CPU iPhone OS 14_7_1 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/14.1.2 Mobile/15E148 Safari/604.1'
        self.desktop_user_agent = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'

    def _create_session(self):
        session = requests.Session()
        retry_strategy = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=[500, 502, 503, 504]
        )
        adapter = HTTPAdapter(max_retries=retry_strategy)
        session.mount("http://", adapter)
        session.mount("https://", adapter)
        return session

    def _scrape_page(self, url, base_domain):
        """Scrape a single page with both mobile and desktop user agents"""
        page_data = {
            'mobile_content': None,
            'desktop_content': None,
            'html': None,
            'links': set()
        }
        
        try:
            # Mobile-first crawl
            self.session.headers.update({'User-Agent': self.mobile_user_agent})
            mobile_response = self.session.get(url, timeout=15, verify=False)
            mobile_response.raise_for_status()
            
            # Desktop crawl
            self.session.headers.update({'User-Agent': self.desktop_user_agent})
            desktop_response = self.session.get(url, timeout=15, verify=False)
            desktop_response.raise_for_status()
            
            # Process responses
            if 'text/html' not in mobile_response.headers.get('content-type', '').lower():
                print(f"Skipping non-HTML content at {url}")
                return None
            
            # Parse mobile content
            mobile_soup = BeautifulSoup(mobile_response.text, "html.parser")
            page_data['mobile_content'] = mobile_soup.get_text(separator=' ').strip()
            
            # Parse desktop content
            desktop_soup = BeautifulSoup(desktop_response.text, "html.parser")
            page_data['desktop_content'] = desktop_soup.get_text(separator=' ').strip()
            
            # Store original HTML for SEO analysis
            page_data['html'] = mobile_response.text  # Store mobile-first HTML
            
            # Extract links (from mobile version, following mobile-first indexing)
            for a in mobile_soup.find_all('a', href=True):
                full_url = urljoin(url, a['href'])
                parsed_url = urlparse(full_url)
                if parsed_url.netloc == base_domain:
                    page_data['links'].add(full_url)
            
            print(f"Successfully scraped {url} (Found {len(page_data['links'])} internal links)")
            
            # Respect robots.txt through delay
            time.sleep(1)
            
            return page_data
            
        except Exception as e:
            print(f"Error processing {url}: {str(e)}")
            return None

    def scrape_site(self, start_url):
        """Scrape entire site starting from the given URL"""
        visited = set()
        content = {}
        pages_attempted = 0
        pages_successful = 0
        
        base_domain = urlparse(start_url).netloc
        print(f"Starting scrape of domain: {base_domain}")
        
        to_visit = [start_url]
        while to_visit and len(visited) < 100:  # Limit to 100 pages
            current_url = to_visit.pop(0)
            if current_url in visited:
                continue
            
            visited.add(current_url)
            pages_attempted += 1
            
            page_data = self._scrape_page(current_url, base_domain)
            if page_data:
                content[current_url] = page_data
                to_visit.extend([link for link in page_data['links'] if link not in visited])
                pages_successful += 1
        
        print(f"\nScraping Summary:")
        print(f"Pages attempted: {pages_attempted}")
        print(f"Pages successful: {pages_successful}")
        print(f"Total unique URLs found: {len(visited)}")
        print(f"Pages with content: {len(content)}")
        
        return content
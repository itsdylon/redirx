from bs4 import BeautifulSoup
from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.metrics.pairwise import cosine_similarity

class ContentAnalyzer:
    def __init__(self):
        self.vectorizer = TfidfVectorizer(
            ngram_range=(1, 2),
            stop_words='english'
        )
        
        # Define weights for different content elements
        self.weights = {
            'title': 3.0,      # Title tag has highest weight
            'h1': 2.5,         # Main heading
            'meta_desc': 2.0,  # Meta description
            'h2': 1.5,         # Subheadings
            'h3': 1.2,         # Minor headings
            'content': 1.0     # Regular content
        }

    def extract_elements(self, html):
        """Extract and weight different elements from HTML"""
        soup = BeautifulSoup(html, 'html.parser')
        elements = {}
        
        # Extract title
        elements['title'] = self._get_text(soup.title) if soup.title else ""
        
        # Extract meta description
        meta_desc = soup.find('meta', attrs={'name': 'description'})
        elements['meta_desc'] = meta_desc.get('content', '') if meta_desc else ""
        
        # Extract headings
        elements['h1'] = self._get_text(soup.find_all('h1'))
        elements['h2'] = self._get_text(soup.find_all('h2'))
        elements['h3'] = self._get_text(soup.find_all('h3'))
        
        # Extract main content
        main_content = soup.find(['main', 'article'])
        if main_content:
            elements['content'] = self._get_text(main_content)
        else:
            # Fallback to body content with common elements removed
            for elem in soup.find_all(['header', 'footer', 'nav', 'aside']):
                elem.decompose()
            elements['content'] = self._get_text(soup.body) if soup.body else ""
        
        return elements

    def _get_text(self, elements):
        """Extract text from elements, handling both single elements and lists"""
        if not elements:
            return ""
        if not isinstance(elements, list):
            elements = [elements]
        return " ".join(elem.get_text(strip=True) for elem in elements if elem)

    def calculate_similarity(self, html1, html2):
        """Calculate weighted similarity between two HTML documents"""
        # Extract elements from both pages
        elements1 = self.extract_elements(html1)
        elements2 = self.extract_elements(html2)
        
        # Calculate individual similarities for each element type
        similarities = {}
        total_weight = 0
        weighted_sum = 0
        
        for elem_type, weight in self.weights.items():
            if elements1[elem_type] and elements2[elem_type]:
                # Calculate TF-IDF similarity for this element
                try:
                    tfidf = self.vectorizer.fit_transform([
                        elements1[elem_type],
                        elements2[elem_type]
                    ])
                    sim = cosine_similarity(tfidf[0:1], tfidf[1:2])[0][0]
                except:
                    # Handle case where vectorization fails (e.g., empty strings)
                    sim = 0.0
                
                # Add to weighted sum
                weighted_sum += sim * weight
                total_weight += weight
                
                similarities[elem_type] = sim
            else:
                similarities[elem_type] = 0.0
        
        # Calculate final weighted similarity
        final_similarity = weighted_sum / total_weight if total_weight > 0 else 0
        
        return {
            'overall_similarity': final_similarity,
            'element_similarities': similarities
        }

    def _clean_text(self, text):
        """Clean text by removing extra whitespace and normalizing"""
        if not text:
            return ""
        # Remove extra whitespace
        text = ' '.join(text.split())
        # Convert to lowercase
        text = text.lower()
        # Remove punctuation (optional)
        # text = re.sub(r'[^\w\s]', '', text)
        return text

    def _calculate_element_similarity(self, text1, text2):
        """Calculate similarity between two text elements using TF-IDF"""
        if not text1 or not text2:
            return 0.0
        
        try:
            tfidf = self.vectorizer.fit_transform([text1, text2])
            return cosine_similarity(tfidf[0:1], tfidf[1:2])[0][0]
        except:
            return 0.0
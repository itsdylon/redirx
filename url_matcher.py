from urllib.parse import urlparse, unquote
import re

class URLMatcher:
    def normalize_url(self, url):
        """Normalize URL for comparison by removing domain and standardizing format"""
        parsed = urlparse(url)
        path = unquote(parsed.path).lower()  # Decode URL encoding and convert to lowercase
        
        # Remove trailing slashes
        path = path.rstrip('/')
        
        # Remove common index files
        path = re.sub(r'/(index|default|home)\.(html|htm|php|asp)$', '', path)
        
        # Remove common URL noise
        path = re.sub(r'\?.*$', '', path)  # Remove query parameters
        path = re.sub(r'#.*$', '', path)   # Remove hash fragments
        
        return path

    def get_url_segments(self, url):
        """Get meaningful segments from URL for partial matching"""
        path = self.normalize_url(url)
        
        # Split path into segments and filter out empty ones
        segments = [s for s in path.split('/') if s]
        
        # Extract potential keywords from segments
        keywords = []
        for segment in segments:
            # Split on common separators and clean
            words = re.split(r'[-_]', segment)
            keywords.extend([w for w in words if w])
        
        return {
            'full_path': path,
            'segments': segments,
            'keywords': keywords
        }

    def calculate_url_similarity(self, url1, url2):
        """Calculate similarity score between two URLs"""
        url1_data = self.get_url_segments(url1)
        url2_data = self.get_url_segments(url2)
        
        # Exact path match
        if url1_data['full_path'] == url2_data['full_path']:
            return 1.0
        
        # Calculate segment matches
        segment_matches = len(set(url1_data['segments']) & set(url2_data['segments']))
        total_segments = max(len(url1_data['segments']), len(url2_data['segments']))
        
        # Calculate keyword matches
        keyword_matches = len(set(url1_data['keywords']) & set(url2_data['keywords']))
        total_keywords = max(len(url1_data['keywords']), len(url2_data['keywords']))
        
        # Weight exact segment matches higher than keyword matches
        if total_segments == 0 and total_keywords == 0:
            return 0.0
        
        segment_score = segment_matches / total_segments if total_segments > 0 else 0
        keyword_score = keyword_matches / total_keywords if total_keywords > 0 else 0
        
        # Weigh segment matches more heavily than keyword matches
        final_score = (segment_score * 0.7) + (keyword_score * 0.3)
        
        return final_score
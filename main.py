from flask import Flask, request, jsonify
from flask_cors import CORS
from scraper import Scraper
from url_matcher import URLMatcher
from content_analyzer import ContentAnalyzer

app = Flask(__name__)
CORS(app)

@app.route("/compare-sites", methods=['POST'])
def compare_sites():
    data = request.json
    old_site_url = data.get('old_site')
    new_site_url = data.get('new_site')

    if not old_site_url or not new_site_url:
        return jsonify({"error": "Both old_site and new_site URLs are required"}), 400
    
    try:
        scraper = Scraper()
        url_matcher = URLMatcher()
        content_analyzer = ContentAnalyzer()
        
        print(f"\nScraping old site: {old_site_url}")
        old_pages = scraper.scrape_site(old_site_url)
        print(f"\nScraping new site: {new_site_url}")
        new_pages = scraper.scrape_site(new_site_url)

        if not old_pages:
            return jsonify({"error": f"No pages scraped from old site: {old_site_url}"}), 500
        if not new_pages:
            return jsonify({"error": f"No pages scraped from new site: {new_site_url}"}), 500

        results = {}
        
        # Process each old URL
        for old_url, old_page_data in old_pages.items():
            # First check for URL pattern matches
            best_match = {"url": None, "similarity": 0, "match_type": None}
            
            # Check for exact slug matches first
            for new_url in new_pages.keys():
                if url_matcher.normalize_url(old_url) == url_matcher.normalize_url(new_url):
                    best_match = {
                        "url": new_url,
                        "similarity": 1.0,
                        "match_type": "exact_url",
                        "redirect_needed": False
                    }
                    break
            
            # If no exact match, try content matching
            if not best_match["url"]:
                url_similarities = {}
                
                # First check URL pattern similarities
                for new_url in new_pages.keys():
                    url_sim = url_matcher.calculate_url_similarity(old_url, new_url)
                    url_similarities[new_url] = url_sim
                
                # Get top URL matches for content comparison
                top_url_matches = sorted(
                    url_similarities.items(),
                    key=lambda x: x[1],
                    reverse=True
                )[:5]  # Only check content for top 5 URL matches
                
                # Check content similarity for top URL matches
                for new_url, url_sim in top_url_matches:
                    content_sim = content_analyzer.calculate_similarity(
                        old_page_data['html'],
                        new_pages[new_url]['html']
                    )
                    
                    # Combined score: 30% URL similarity, 70% content similarity
                    combined_sim = (url_sim * 0.3) + (content_sim['overall_similarity'] * 0.7)
                    
                    if combined_sim > best_match["similarity"]:
                        best_match = {
                            "url": new_url,
                            "similarity": combined_sim,
                            "match_type": "content",
                            "redirect_needed": True,
                            "url_similarity": url_sim,
                            "content_similarity": content_sim['overall_similarity'],
                            "element_matches": content_sim['element_similarities']
                        }
            
            results[old_url] = best_match
            print(f"Processed {old_url} -> {best_match['url']} "
                  f"(similarity: {best_match['similarity']:.2f}, "
                  f"type: {best_match['match_type']})")

        return jsonify(results)
    
    except Exception as e:
        print(f"Error in compare_sites: {str(e)}")
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(debug=True)
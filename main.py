from flask import Flask, request, jsonify
from flask_cors import CORS
from scraper import Scraper
from url_matcher import URLMatcher
from content_analyzer import ContentAnalyzer
import os

app = Flask(__name__)

#configure CORS
cors_origins = [
    "https://redirx-iota.vercel.app/"
]

# Enable CORS for all origins during testing
CORS(app, resources={
    r"/*": {
        "origins": "*",  # Allow all origins temporarily
        "methods": ["GET", "POST", "OPTIONS"],
        "allow_headers": ["Content-Type", "Authorization"],
        "expose_headers": ["Content-Type", "Authorization"],
        "max_age": 3600,
        "supports_credentials": True
    }
})

@app.route("/", methods=['GET', 'OPTIONS'])
def health_check():
    return jsonify({"status": "ok"})

@app.after_request
def after_request(response):
    response.headers.add('Access-Control-Allow-Origin', '*')
    response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization')
    response.headers.add('Access-Control-Allow-Methods', 'GET,POST,OPTIONS')
    return response

@app.route("/compare-sites", methods=['POST'])
def compare_sites():
    data = request.json
    old_site_url = data.get('old_site')
    new_site_url = data.get('new_site')
    scan_only = data.get('scan_only', False)
    selected_urls = data.get('selected_urls', [])

    if not old_site_url:
        return jsonify({"error": "old_site URL is required"}), 400
    
    try:
        scraper = Scraper()
        url_matcher = URLMatcher()
        content_analyzer = ContentAnalyzer()
        
        print(f"\nScraping old site: {old_site_url}")
        old_pages = scraper.scrape_site(old_site_url)

        if not old_pages:
            return jsonify({"error": f"No pages scraped from old site: {old_site_url}"}), 500

        # If scan_only is True, return just the scraped pages
        if scan_only:
            # Convert sets to lists in the response
            serializable_pages = {}
            for url, page_data in old_pages.items():
                serializable_pages[url] = {
                    'mobile_content': page_data['mobile_content'],
                    'desktop_content': page_data['desktop_content'],
                    'html': page_data['html'],
                    'links': list(page_data['links'])  # Convert set to list
                }
            return jsonify(serializable_pages)

        # For comparison, we need the new site URL
        if not new_site_url:
            return jsonify({"error": "new_site URL is required for comparison"}), 400

        print(f"\nScraping new site: {new_site_url}")
        new_pages = scraper.scrape_site(new_site_url)

        if not new_pages:
            return jsonify({"error": f"No pages scraped from new site: {new_site_url}"}), 500

        results = {}
        
        # Only process selected URLs if provided, otherwise process all
        urls_to_process = selected_urls if selected_urls else old_pages.keys()
        
        # Process each old URL
        for old_url in urls_to_process:
            if old_url not in old_pages:
                continue

            old_page_data = old_pages[old_url]
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

        # Ensure all data is JSON serializable
        serializable_results = {}
        for url, result in results.items():
            serializable_result = result.copy()
            # Convert any sets to lists if they exist
            if 'element_matches' in serializable_result:
                serializable_result['element_matches'] = {
                    k: list(v) if isinstance(v, set) else v 
                    for k, v in serializable_result['element_matches'].items()
                }
            serializable_results[url] = serializable_result
            
        return jsonify(serializable_results)
    
    except Exception as e:
        print(f"Error in compare_sites: {str(e)}")
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
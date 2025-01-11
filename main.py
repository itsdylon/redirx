from flask import Flask, request, jsonify
from flask_cors import CORS  # Add this import
from scraper import scrape_site
from similarity import compute_similarity

app = Flask(__name__)
CORS(app)  # Enable CORS

@app.route("/compare-sites", methods=['POST'])
def compare_sites():
    data = request.json
    
    old_site_url = data.get('old_site')
    new_site_url = data.get('new_site')

    if not old_site_url or not new_site_url:
        return jsonify({"error": "Both old_site and new_site URLs are required"}), 400
    
    try:
        print(f"\nScraping old site: {old_site_url}")
        old_site_pages = scrape_site(old_site_url)
        print(f"\nScraping new site: {new_site_url}")
        new_site_pages = scrape_site(new_site_url)

        if not old_site_pages:
            return jsonify({"error": f"No pages scraped from old site: {old_site_url}"}), 500
        if not new_site_pages:
            return jsonify({"error": f"No pages scraped from new site: {new_site_url}"}), 500

        print(f"\nComparing {len(old_site_pages)} old pages with {len(new_site_pages)} new pages")
        results = {}
        for old_url, old_content in old_site_pages.items():
            best_match = {"url": None, "similarity": 0}
            for new_url, new_content in new_site_pages.items():
                similarity = compute_similarity(old_content, new_content)
                if similarity > best_match["similarity"]:
                    best_match = {"url": new_url, "similarity": similarity}
            results[old_url] = best_match
            print(f"Found best match for {old_url} -> {best_match['url']} ({best_match['similarity']:.2%})")

        return jsonify(results)
    
    except Exception as e:
        print(f"Error in compare_sites: {str(e)}")
        return jsonify({"error": f"An error occurred: {str(e)}"}), 500

if __name__ == "__main__":
    app.run(debug=True)
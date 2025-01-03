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
    
    #scrape both sites
    old_site_pages = scrape_site(old_site_url)
    new_site_pages = scrape_site(new_site_url)

    if not old_site_pages or not new_site_pages:
        return jsonify({"error": "Failed to scrape one or both sites"}), 500
    
    #compare pages
    results = {}
    for old_url, old_content in old_site_pages.items():
        best_match = {"url": None, "similarity": 0}
        for new_url, new_content in new_site_pages.items():
            similarity = compute_similarity(old_content, new_content)
            if similarity > best_match["similarity"]:
                best_match = {"url": new_url, "similarity": similarity}
        results[old_url] = best_match

    return jsonify(results)

if __name__ == "__main__":
    app.run(debug=True)
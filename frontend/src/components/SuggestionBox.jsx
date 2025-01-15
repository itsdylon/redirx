import React, { useState } from 'react';
import { MessageSquarePlus, Mail, X } from 'lucide-react';

const SuggestionBox = () => {
  const [isOpen, setIsOpen] = useState(false);
  const [suggestion, setSuggestion] = useState('');
  
  const handleSubmit = (e) => {
    e.preventDefault();
    if (suggestion.trim()) {
      const subject = encodeURIComponent('Portfolio Website Suggestion');
      const body = encodeURIComponent(suggestion);
      window.location.href = `mailto:reececoppage@gmail2.com?subject=${subject}&body=${body}`;
      setIsOpen(false);
      setSuggestion('');
    }
  };

  return (
    <div className="fixed top-4 right-4 z-50">
      {!isOpen ? (
        <button
          onClick={() => setIsOpen(true)}
          className="bg-blue-500 text-white p-2 rounded-full shadow-lg hover:bg-blue-600 transition-colors"
          aria-label="Open suggestion box"
        >
          <MessageSquarePlus className="h-6 w-6" />
        </button>
      ) : (
        <div className="bg-white rounded-lg shadow-xl p-4 w-72">
          <div className="flex justify-between items-center mb-3">
            <h3 className="font-semibold text-gray-700">Send Suggestion</h3>
            <button
              onClick={() => setIsOpen(false)}
              className="text-gray-400 hover:text-gray-600 transition-colors"
              aria-label="Close suggestion box"
            >
              <X className="h-5 w-5" />
            </button>
          </div>
          
          <form onSubmit={handleSubmit} className="space-y-3">
            <textarea
              value={suggestion}
              onChange={(e) => setSuggestion(e.target.value)}
              placeholder="What would you like to suggest?"
              className="w-full h-24 p-2 text-sm border rounded-md focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-gray-900"
              required
            />
            <button
              type="submit"
              className="w-full bg-blue-500 text-white py-2 px-4 rounded-md hover:bg-blue-600 transition-colors flex items-center justify-center gap-2"
            >
              <Mail className="h-4 w-4" />
              <span>Send Suggestion</span>
            </button>
          </form>
        </div>
      )}
    </div>
  );
};

export default SuggestionBox;
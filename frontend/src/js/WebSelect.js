document.addEventListener("DOMContentLoaded", () => {
    const links = [
      { oldUrl: "http://oldsite.com/page1", newUrl: "http://newsite.com/page1" },
      { oldUrl: "http://oldsite.com/page2", newUrl: "http://newsite.com/page2" },
      { oldUrl: "http://oldsite.com/page3", newUrl: "http://newsite.com/page3" },
    ];
  
    const linkTable = document.getElementById("linkTable");
    const selectAllButton = document.getElementById("selectAll");
    const confirmButton = document.getElementById("confirm");
  
    // Populate table
    links.forEach((link, index) => {
      const row = document.createElement("tr");
      row.innerHTML = `
        <td><input type="checkbox" class="redirectCheckbox" data-index="${index}"></td>
        <td>${link.oldUrl}</td>
        <td>${link.newUrl}</td>
      `;
      linkTable.appendChild(row);
    });
  
    // Select All functionality
    selectAllButton.addEventListener("click", () => {
      const checkboxes = document.querySelectorAll(".redirectCheckbox");
      const allChecked = Array.from(checkboxes).every(cb => cb.checked);
      checkboxes.forEach(cb => (cb.checked = !allChecked));
    });
  
    // Confirm functionality
    confirmButton.addEventListener("click", () => {
      const selectedLinks = [];
      document.querySelectorAll(".redirectCheckbox:checked").forEach(cb => {
        const index = cb.dataset.index;
        selectedLinks.push(links[index]);
      });
      console.log("Selected links for redirection:", selectedLinks);
  
      // Send selected links to the backend
      fetch("/update-redirects", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
        },
        body: JSON.stringify(selectedLinks),
      })
        .then(response => response.json())
        .then(data => {
          alert("Redirects updated successfully!");
          console.log(data);
        })
        .catch(error => console.error("Error:", error));
    });
  });
  
(self.webpackChunk_N_E=self.webpackChunk_N_E||[]).push([[974],{4852:(e,r,s)=>{Promise.resolve().then(s.bind(s,6521))},6521:(e,r,s)=>{"use strict";s.d(r,{default:()=>d});var t=s(5155),l=s(2115);let a=e=>e.replace(/([a-z0-9])([A-Z])/g,"$1-$2").toLowerCase(),i=function(){for(var e=arguments.length,r=Array(e),s=0;s<e;s++)r[s]=arguments[s];return r.filter((e,r,s)=>!!e&&""!==e.trim()&&s.indexOf(e)===r).join(" ").trim()};var n={xmlns:"http://www.w3.org/2000/svg",width:24,height:24,viewBox:"0 0 24 24",fill:"none",stroke:"currentColor",strokeWidth:2,strokeLinecap:"round",strokeLinejoin:"round"};let c=(0,l.forwardRef)((e,r)=>{let{color:s="currentColor",size:t=24,strokeWidth:a=2,absoluteStrokeWidth:c,className:o="",children:d,iconNode:m,...u}=e;return(0,l.createElement)("svg",{ref:r,...n,width:t,height:t,stroke:s,strokeWidth:c?24*Number(a)/Number(t):a,className:i("lucide",o),...u},[...m.map(e=>{let[r,s]=e;return(0,l.createElement)(r,s)}),...Array.isArray(d)?d:[d]])}),o=((e,r)=>{let s=(0,l.forwardRef)((s,t)=>{let{className:n,...o}=s;return(0,l.createElement)(c,{ref:t,iconNode:r,className:i("lucide-".concat(a(e)),n),...o})});return s.displayName="".concat(e),s})("CircleAlert",[["circle",{cx:"12",cy:"12",r:"10",key:"1mglay"}],["line",{x1:"12",x2:"12",y1:"8",y2:"12",key:"1pkeuh"}],["line",{x1:"12",x2:"12.01",y1:"16",y2:"16",key:"4dfq90"}]]),d=()=>{let[e,r]=(0,l.useState)(""),[s,a]=(0,l.useState)(""),[i,n]=(0,l.useState)(null),[c,d]=(0,l.useState)(""),[m,u]=(0,l.useState)(!1),h=async r=>{r.preventDefault(),u(!0),d(""),n(null);try{let r=await fetch("/compare-sites",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({old_site:e,new_site:s})});if(!r.ok)throw Error("HTTP error! status: ".concat(r.status));let t=await r.json();n(t)}catch(e){d(e instanceof Error?e.message:"An error occurred")}finally{u(!1)}},x=(e,r)=>{try{let s=new URL(e).pathname,t=new URL(r).pathname;return"RewriteRule ^".concat(s.substring(1),"$ ").concat(t," [R=301,L]")}catch(e){return"Invalid URL"}};return(0,t.jsxs)("div",{className:"max-w-4xl mx-auto p-6 space-y-6",children:[(0,t.jsx)("h1",{className:"text-2xl font-bold mb-6",children:"Website Migration Comparison Tool"}),(0,t.jsxs)("form",{onSubmit:h,className:"space-y-4",children:[(0,t.jsxs)("div",{className:"space-y-2",children:[(0,t.jsx)("label",{htmlFor:"oldSite",className:"block font-medium",children:"Original Site URL:"}),(0,t.jsx)("input",{id:"oldSite",type:"url",value:e,onChange:e=>r(e.target.value),required:!0,className:"w-full p-2 border rounded focus:ring-2 focus:ring-blue-500",placeholder:"https://old-site.com"})]}),(0,t.jsxs)("div",{className:"space-y-2",children:[(0,t.jsx)("label",{htmlFor:"newSite",className:"block font-medium",children:"New Site URL:"}),(0,t.jsx)("input",{id:"newSite",type:"url",value:s,onChange:e=>a(e.target.value),required:!0,className:"w-full p-2 border rounded focus:ring-2 focus:ring-blue-500",placeholder:"https://new-site.com"})]}),(0,t.jsx)("button",{type:"submit",disabled:m,className:"w-full bg-blue-500 text-white p-2 rounded hover:bg-blue-600 disabled:bg-blue-300",children:m?"Comparing Sites...":"Compare Sites"})]}),c&&(0,t.jsxs)("div",{className:"bg-red-50 border border-red-200 text-red-700 p-4 rounded-md mt-4",children:[(0,t.jsxs)("div",{className:"flex items-center gap-2",children:[(0,t.jsx)(o,{className:"h-4 w-4"}),(0,t.jsx)("p",{className:"font-semibold",children:"Error"})]}),(0,t.jsx)("p",{className:"mt-1",children:c})]}),i&&(0,t.jsxs)("div",{className:"mt-6",children:[(0,t.jsx)("h2",{className:"text-xl font-semibold mb-4",children:"Comparison Results"}),(0,t.jsx)("div",{className:"overflow-x-auto",children:(0,t.jsxs)("table",{className:"min-w-full border-collapse border",children:[(0,t.jsx)("thead",{children:(0,t.jsxs)("tr",{className:"bg-gray-100",children:[(0,t.jsx)("th",{className:"border p-2",children:"Original URL"}),(0,t.jsx)("th",{className:"border p-2",children:"Best Match URL"}),(0,t.jsx)("th",{className:"border p-2",children:"Similarity"}),(0,t.jsx)("th",{className:"border p-2",children:"Redirect Rule"})]})}),(0,t.jsx)("tbody",{children:Object.entries(i).map(e=>{let[r,s]=e;return(0,t.jsxs)("tr",{children:[(0,t.jsx)("td",{className:"border p-2 break-all",children:r}),(0,t.jsx)("td",{className:"border p-2 break-all",children:s.url}),(0,t.jsxs)("td",{className:"border p-2 text-center",children:[(100*s.similarity).toFixed(1),"%"]}),(0,t.jsx)("td",{className:"border p-2 font-mono text-sm break-all",children:s.url&&x(r,s.url)})]},r)})})]})})]})]})}}},e=>{var r=r=>e(e.s=r);e.O(0,[441,517,358],()=>r(4852)),_N_E=e.O()}]);
document.getElementById("start-study").addEventListener("click",async()=>{
  try{await AssistantClient.api("/api/desktop/welcomed",{method:"POST",body:{}});location.href="study.html";}
  catch(e){document.getElementById("start-note").textContent=e.message;}
});
document.querySelectorAll('a[href="study.html"]').forEach(link=>link.addEventListener("click",async event=>{
  event.preventDefault();await AssistantClient.api("/api/desktop/welcomed",{method:"POST",body:{}}).catch(()=>{});location.href="study.html";
}));

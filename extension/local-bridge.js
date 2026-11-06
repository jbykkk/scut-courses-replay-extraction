// Only matched on the local app's fixed origin. No page messages are accepted.
chrome.runtime.sendMessage({type:"LOCAL_APP_READY"}).catch(()=>{});
document.addEventListener("visibilitychange",()=>{
  if(!document.hidden)chrome.runtime.sendMessage({type:"LOCAL_APP_READY"}).catch(()=>{});
});

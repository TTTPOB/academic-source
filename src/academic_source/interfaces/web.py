"""A small client of the public HTTP API."""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse


def create_web_router() -> APIRouter:
    router = APIRouter()

    @router.get("/", response_class=HTMLResponse)
    def home() -> str:
        return """<!doctype html>
<html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Academic Source</title>
<style>body{font:16px system-ui;max-width:52rem;margin:3rem auto;padding:0 1rem;line-height:1.5}
input,textarea,button{font:inherit;padding:.5rem}textarea{width:100%;box-sizing:border-box}
button{cursor:pointer}pre{white-space:pre-wrap;overflow-wrap:anywhere}a{display:block}</style>
<h1>Academic Source</h1><p>Enter an identifier on each line, or upload a literature list.</p>
<textarea id="ids" rows="5" placeholder="10.1000/example"></textarea><p><input id="file" type="file">
<button id="go">Acquire</button></p><p id="status"></p><div id="files"></div>
<script>
const status=document.querySelector('#status'), files=document.querySelector('#files');
async function api(path,options){const r=await fetch('/api/v1/'+path,options);const data=await r.json();
 if(!r.ok)throw Error(JSON.stringify(data));return data}
document.querySelector('#go').onclick=async()=>{const button=document.querySelector('#go');button.disabled=true;files.replaceChildren();status.textContent='Submitting…';
 try{const file=document.querySelector('#file').files[0];let input;
 if(file){const form=new FormData();form.append('file',file);const upload=await api('uploads',{method:'POST',body:form});input={upload_id:upload.id}}
 else{input={identifiers:document.querySelector('#ids').value.split(/\\r?\\n/).map(s=>s.trim()).filter(Boolean)}}
 let job=await api('acquisitions',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(input)});
 while(['queued','running'].includes(job.status)){status.textContent='Job '+job.id+': '+job.status+' ('+job.completed+'/'+job.total+')';
 await new Promise(resolve=>setTimeout(resolve,1000));job=await api('jobs/'+encodeURIComponent(job.id))}
 status.textContent='Job '+job.id+': '+job.status+' ('+job.completed+'/'+job.total+')';
 for(const item of job.artifacts){const link=document.createElement('a');link.href=item.download_url;link.textContent='Download '+item.filename;files.append(link)}
 for(const result of job.results){if(result.status==='failed'){const note=document.createElement('p');note.textContent=result.identifier+': '+result.reason;files.append(note)}}
 if(job.error)status.textContent+=' — '+job.error;
 }catch(error){status.textContent=String(error)}finally{button.disabled=false}};
</script></html>"""

    return router

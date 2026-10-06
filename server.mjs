import http from 'node:http';
import { readFile } from 'node:fs/promises';
const files={'/':'index.html','/index.html':'index.html','/app.js':'app.js','/styles.css':'styles.css','/results.json':'results.json'};
const types={html:'text/html; charset=utf-8',js:'text/javascript; charset=utf-8',css:'text/css; charset=utf-8',json:'application/json'};
const port=Number(process.env.PORT||4173);
http.createServer(async(req,res)=>{const pathname=new URL(req.url,'http://localhost').pathname;const file=files[pathname];if(!file){res.writeHead(404);res.end('Not found');return;}try{const body=await readFile(new URL(file,import.meta.url));res.writeHead(200,{'Content-Type':types[file.split('.').pop()],'Cache-Control':'no-store'});res.end(body);}catch{res.writeHead(500);res.end('Could not read project file');}}).listen(port,'127.0.0.1',()=>console.log(`Switchyard preview: http://localhost:${port}`));

const key=endpoint=>`resolvedesk.login:${endpoint}`
export function readSession(endpoint,storage,now=Date.now()/1000){
  try{const saved=JSON.parse(storage.getItem(key(endpoint))||'null');if(!saved?.token||saved.expires_at<=now){storage.removeItem(key(endpoint));return null}return saved}catch{return null}
}
export function saveSession(endpoint,session,storage){storage.setItem(key(endpoint),JSON.stringify({token:session.token,expires_at:session.expires_at}))}
export function clearSession(endpoint,storage){storage.removeItem(key(endpoint))}

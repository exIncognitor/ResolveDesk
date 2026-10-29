"""Add login accounts and an encryption key without resetting existing identities/data."""
import hashlib
import json
import os
from pathlib import Path
import secrets

from cryptography.fernet import Fernet
from dotenv import load_dotenv

from resolvedesk.identity import IdentityService
from resolvedesk.settings import ServiceSettings
from resolvedesk.store import Principal,Store

ROOT=Path(__file__).resolve().parents[1]


def main():
    load_dotenv(ROOT/'.env')
    path=ROOT/'data/config-encryption.key'
    if not os.getenv('RESOLVEDESK_CONFIG_ENCRYPTION_KEY') and not path.exists():
        with path.open('xb') as out: out.write(Fernet.generate_key())
        path.chmod(0o600)
    store=Store(ServiceSettings.load().database_url)
    service=IdentityService(store)
    output=ROOT/'.codex-runtime/login-accounts.json'
    accounts={}
    try:
        for user,role in (('learner','user'),('support','support')):
            if service.member('demo',user): continue
            password=secrets.token_urlsafe(18)
            service.create_account(Principal('demo','bootstrap','support'),user,password,role,'员工' if role=='user' else '支持人员')
            accounts[user]={'tenant':'demo','username':user,'password':password,'role':role}
        # Existing local API tokens receive a fixed expiry and DB revocation checks.
        old=ROOT/'data/auth.json'
        if old.exists():
            values=json.loads(old.read_text(encoding='utf-8'))
            with store.db(True) as db:
                for hashed,actor in values.items():
                    row=db.execute('SELECT * FROM desk_accounts WHERE tenant=? AND user_id=?',(actor['tenant'],actor['user'])).fetchone()
                    if row and len(hashed)==64:
                        db.execute("INSERT INTO desk_sessions VALUES(?,?,?,?,?,0,'api',?) ON CONFLICT(token_hash) DO NOTHING",
                                   (hashed,row['tenant'],row['user_id'],row['epoch'],db.now()+7*86400,db.now()))
        if accounts:
            # Never replace an earlier credentials file, even after partial setup.
            if output.exists(): output=output.with_name('login-accounts-'+secrets.token_hex(6)+'.json')
            with output.open('x',encoding='utf-8') as out: json.dump(accounts,out,ensure_ascii=False,indent=2)
            output.chmod(0o600)
        print(json.dumps({'new_accounts':list(accounts),'credentials_file':str(output) if accounts else 'existing credentials preserved','key_present':path.exists() or bool(os.getenv('RESOLVEDESK_CONFIG_ENCRYPTION_KEY'))},ensure_ascii=False))
    finally: store.close()


if __name__=='__main__': main()

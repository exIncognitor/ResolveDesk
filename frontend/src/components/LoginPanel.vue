<template>
  <main class="login-page">
    <section class="login-intro"><span class="login-brand">RD · ResolveDesk</span><span class="kicker">YOUR IT SERVICE DESK</span><h1>每一个 IT 需求，<br />都有清晰的下一步。</h1><p>员工提交需求、查询知识和跟进申请；支持人员审批、处理并管理服务。</p><div class="login-path"><span>提出需求</span><span>核对申请</span><span>协同办理</span><span>追踪结果</span></div></section>
    <section class="login-card"><span class="kicker">WELCOME BACK</span><h2>登录工作台</h2><p class="muted">使用你的组织账号登录，功能由服务端身份决定。</p>
      <p v-if="error || notice" class="login-error" role="alert">{{ error || notice }}</p>
      <form @submit.prevent="login"><fieldset :disabled="busy"><label>组织标识<input v-model="tenant" required maxlength="64" autocomplete="organization" /></label><label>账号<input v-model="username" required maxlength="64" autocomplete="username" placeholder="输入账号" /></label><label>密码<input v-model="password" required type="password" maxlength="256" autocomplete="current-password" placeholder="输入密码" /></label><button class="login-submit" type="submit">{{ busy ? '正在登录…' : '登录工作台' }}</button></fieldset></form>
      <div v-if="demoEnabled" class="demo-entry"><strong>本地测试 · 快速体验</strong><p>测试开关已开启，可直接切换两种演示身份。生产模式会禁用此入口。</p><div><button class="quiet-button" :disabled="busy" @click="demo('user')">以员工身份体验</button><button class="quiet-button" :disabled="busy" @click="demo('support')">以支持人员身份体验</button></div></div>
      <small class="muted">模型 API Key 由支持人员在后台管理，请勿将其作为登录密码。</small>
    </section>
  </main>
</template>

<script setup>
import { onMounted, onBeforeUnmount, ref } from 'vue'
import { deskRequest } from '../lib/resolvedesk.js'
import { createRequestState } from '../lib/requestState.js'
const props=defineProps({settings:{type:Object,required:true},notice:{type:String,default:''}})
const emit=defineEmits(['authenticated'])
const tenant=ref('demo'),username=ref(''),password=ref(''),error=ref(''),busy=ref(false),demoEnabled=ref(false)
const requests=createRequestState(props.settings)
async function submit(path,body){
  const r=requests.begin('login');busy.value=true;error.value=''
  try { const result=await deskRequest(r.settings,path,'POST',body);if(r.isCurrent()){password.value='';emit('authenticated',result)} }
  catch(e){if(r.isCurrent())error.value=e.message}
  finally{if(r.isCurrent())busy.value=false}
}
function login(){return submit('/auth/login',{tenant:tenant.value.trim(),username:username.value.trim(),password:password.value})}
function demo(role){return submit('/auth/demo-login',{role})}
onMounted(async()=>{const r=requests.begin('options');try{const o=await deskRequest(r.settings,'/auth/options');if(r.isCurrent())demoEnabled.value=!!o.demo_login_enabled}catch(e){if(r.isCurrent())error.value='无法连接服务，请检查服务是否已启动。'}})
onBeforeUnmount(()=>requests.invalidate())
</script>

<style scoped>
.login-page{min-height:100dvh;display:grid;grid-template-columns:1.15fr 1fr;background:#f5f7fc;color:#15213b}.login-intro{padding:clamp(40px,7vw,120px);display:flex;flex-direction:column;justify-content:center;background:linear-gradient(145deg,#eaf0ff,#f9fbff);gap:24px}.login-brand{font-size:22px;font-weight:800;margin-bottom:64px}.login-intro h1{font-size:clamp(32px,4vw,56px);line-height:1.25;letter-spacing:-1.5px;margin:0}.login-intro p{max-width:480px;font-size:17px;line-height:1.9;color:#647087}.login-path{display:flex;flex-wrap:wrap;gap:14px;font-size:13px}.login-path span{border-left:3px solid #345bde;padding:4px 12px}.login-card{margin:auto;padding:42px;width:min(500px,100%);box-sizing:border-box}.login-card h2{font-size:30px;margin:12px 0}.login-card form{margin:26px 0}.login-card fieldset{padding:0;border:0;display:grid;gap:18px}.login-card label{display:grid;gap:8px;font-size:14px;font-weight:600}.login-card input{width:100%;box-sizing:border-box;background:white;min-height:46px;padding:10px 12px;border:1px solid #d9dfec;border-radius:9px}.login-submit{justify-content:center;min-height:46px}.demo-entry{border-top:1px solid #dce3f0;padding-top:20px;margin-bottom:25px}.demo-entry p{font-size:13px;line-height:1.7;color:#68758a}.demo-entry>div{display:flex;gap:10px;flex-wrap:wrap}.login-error{background:#fff0ed;color:#a73320;padding:12px;border-radius:8px;line-height:1.6}.login-card>small{display:block;line-height:1.8}@media(max-width:850px){.login-page{grid-template-columns:1fr}.login-intro{padding:28px 28px 10px;gap:10px}.login-brand{margin-bottom:12px}.login-intro h1{font-size:30px}.login-intro p,.login-path{display:none}.login-card{padding:28px;max-width:520px}}
</style>

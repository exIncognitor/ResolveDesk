<template>
  <section class="workspace-card knowledge-library" aria-label="已发布文档目录">
    <div class="card-heading"><div><span class="kicker">DOCUMENT LIBRARY</span><h2>已发布文档 <small>{{ catalog.total }} 篇</small></h2></div><button class="quiet-button" :disabled="loading" @click="load()">刷新目录</button></div>
    <p class="muted">这里显示当前账号有权阅读且仍在有效期内的文档。点击卡片查看正文，下面的检索区用于按问题查找知识。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="loading" role="status">正在加载文档目录…</p>
    <div v-else class="document-grid">
      <button v-for="document in catalog.items" :key="document.document_id" class="document-card" :aria-label="`查看 ${document.title}`" @click="open(document)"><AppIcon name="document" /><strong>{{ document.title }}</strong><small>{{ document.chunks }} 个知识片段 · 查看正文</small></button>
    </div>
    <p v-if="!loading && !error && !catalog.total" class="muted">当前没有可读的已发布文档。新导入的草稿需经支持人员审核发布后才会显示。</p>
    <div v-if="catalog.total > catalog.limit" class="library-pagination"><button class="quiet-button" :disabled="loading || !catalog.offset" @click="load(catalog.offset-catalog.limit)">上一页文档</button><span>{{ catalog.offset+1 }}–{{ Math.min(catalog.offset+catalog.limit,catalog.total) }} / {{ catalog.total }}</span><button class="quiet-button" :disabled="loading || catalog.offset+catalog.limit>=catalog.total" @click="load(catalog.offset+catalog.limit)">下一页文档</button></div>
    <section v-if="selected" class="document-preview" aria-label="文档正文预览">
      <div class="card-heading"><h3>{{ selected.title }}</h3><button class="text-button" @click="closePreview">关闭预览</button></div>
      <p v-if="previewLoading" role="status">正在加载正文…</p>
      <template v-if="preview"><p class="muted">按原文片段显示；长文档可继续翻页。</p><div class="document-sections"><article v-for="(section,index) in preview.items" :key="`${preview.offset}-${index}`"><h4>{{ section.section || '正文' }}</h4><p>{{ section.content }}</p></article></div><div v-if="preview.total>preview.limit" class="library-pagination"><button class="quiet-button" :disabled="previewLoading || !preview.offset" @click="open(selected,preview.offset-preview.limit)">前一组片段</button><span>{{ preview.offset+1 }}–{{ Math.min(preview.offset+preview.limit,preview.total) }} / {{ preview.total }} 个片段</span><button class="quiet-button" :disabled="previewLoading || preview.offset+preview.limit>=preview.total" @click="open(selected,preview.offset+preview.limit)">后一组片段</button></div></template>
    </section>
  </section>
</template>

<script setup>
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import AppIcon from './AppIcon.vue'
import { deskRequest } from '../lib/resolvedesk'
import { createRequestState } from '../lib/requestState'
const props=defineProps({settings:{type:Object,required:true}})
const requests=createRequestState(props.settings)
const catalog=ref({items:[],total:0,limit:6,offset:0}),selected=ref(null),preview=ref(null)
const loading=ref(false),previewLoading=ref(false),error=ref('')
function reset(){requests.invalidate();catalog.value={items:[],total:0,limit:6,offset:0};selected.value=null;preview.value=null;loading.value=false;previewLoading.value=false;error.value=''}
async function load(offset=0){
  requests.invalidate();selected.value=null;preview.value=null;previewLoading.value=false
  const r=requests.begin('catalog');loading.value=true;error.value=''
  try{const data=await deskRequest(r.settings,`/knowledge/documents?limit=6&offset=${offset}`);if(r.isCurrent())catalog.value=data}
  catch(e){if(r.isCurrent()){catalog.value={items:[],total:0,limit:6,offset:0};error.value=`文档目录加载失败：${e.message}`}}
  finally{if(r.isCurrent())loading.value=false}
}
async function open(document,offset=0){
  const r=requests.begin('preview');selected.value=document;preview.value=null;previewLoading.value=true;error.value=''
  try{const data=await deskRequest(r.settings,`/knowledge/documents/${encodeURIComponent(document.document_id)}?limit=6&offset=${offset}`);if(r.isCurrent())preview.value=data}
  catch(e){if(r.isCurrent()){selected.value=null;error.value=`正文暂不可读，请刷新目录：${e.message}`}}
  finally{if(r.isCurrent())previewLoading.value=false}
}
function closePreview(){requests.begin('preview');selected.value=null;preview.value=null;previewLoading.value=false}
watch(()=>[props.settings.pythonToken,props.settings.endpoints?.python],()=>{reset();load()})
onMounted(load)
onBeforeUnmount(reset)
defineExpose({load})
</script>

<style scoped>
.knowledge-library{margin-bottom:24px}.knowledge-library>.muted{font-size:12px;line-height:1.8;margin-bottom:18px}.card-heading h2 small{font-size:13px;font-weight:400;color:var(--muted);margin-left:10px}.document-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,240px),1fr));gap:12px}.document-card{display:grid;grid-template-columns:22px 1fr;gap:8px 10px;align-content:start;justify-content:initial;text-align:left;background:var(--surface,#fff);border:1px solid var(--line);color:var(--ink);padding:16px;min-width:0}.document-card:hover{border-color:#7294ed;background:#f6f8ff}.document-card .icon{width:18px;height:18px;color:#4569d4}.document-card strong{font-size:13px;line-height:1.6;overflow-wrap:anywhere}.document-card small{grid-column:2;font-size:11px;color:var(--muted);font-weight:400}.library-pagination{display:flex;gap:12px;align-items:center;justify-content:flex-end;flex-wrap:wrap;margin-top:16px;font-size:12px;color:var(--muted)}.document-preview{border-top:1px solid var(--line);margin-top:20px;padding-top:20px}.document-preview h3{font-size:16px;overflow-wrap:anywhere}.document-sections{max-height:480px;overflow:auto;margin-top:14px}.document-sections article{padding:12px 0;border-bottom:1px solid var(--line)}.document-sections h4{font-size:13px;margin:0 0 10px}.document-sections article p{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.85;font-size:13px}.document-preview>.muted{font-size:12px;line-height:1.8}
</style>

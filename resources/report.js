
const q=(s,r=document)=>[...r.querySelectorAll(s)];
let mode='all', term='';
function apply(){
  q('.trial').forEach(t=>{
    const st=t.dataset.status, txt=t.dataset.text;
    let ok = mode==='all' || (mode==='fail'&&st==='fail') ||
             (mode==='refuse'&&st==='refuse') || (mode==='problem'&&st!=='pass');
    if(ok && term) ok = txt.includes(term);
    t.classList.toggle('hidden', !ok);
  });
  q('.task').forEach(sec=>{
    const any=q('.trial:not(.hidden)',sec).length>0;
    sec.style.display = any ? '' : 'none';
  });
}
document.addEventListener('DOMContentLoaded',()=>{
  q('.controls button').forEach(b=>b.onclick=()=>{
    q('.controls button').forEach(x=>x.classList.remove('on'));
    b.classList.add('on'); mode=b.dataset.mode; apply();
  });
  const inp=document.getElementById('search');
  inp.oninput=()=>{ term=inp.value.toLowerCase(); apply(); };
  apply();
});

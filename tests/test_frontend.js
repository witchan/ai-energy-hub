// Dependency-free DOM contract tests. No browser/network or real credentials.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const html = fs.readFileSync(path.join(__dirname, '..', 'index.html'), 'utf8');
const script = html.split('<script>')[1].split('</script>')[0].replace(/initialize\(\);\s*$/, '');

function setup(brokenStorage = false, storage = new Map()) {
    const ids = new Map();
    const rendered = [];
    class Element {
        constructor(tag) { this.tagName = tag; this.children = []; this.style = {}; this.value = ''; this.textContent = ''; this.disabled = false; this.dataset = {}; this.classList = {add(){},remove(){},toggle(){}}; }
        append(...children) { this.children.push(...children); }
        appendChild(child) { this.append(child); return child; }
        replaceChildren(...children) { this.children = children; }
        remove() { this.removed = true; }
        querySelectorAll() { return []; }
        querySelector() { return new Element('button'); }
        setAttribute(name, value) { this[name] = value; }
        scrollIntoView() {}
        set innerHTML(value) {
            this.html = value;
            rendered.push(value);
            for (const match of value.matchAll(/id="([^"]+)"/g)) ids.set(match[1], new Element('div'));
            this.firstElementChild = new Element('div');
        }
        get innerHTML() { return this.html || ''; }
    }
    const events = new Map();
    const get = id => { if (!ids.has(id)) ids.set(id, new Element('div')); return ids.get(id); };
    const session = new Map();
    const context = {
        crypto: require('node:crypto').webcrypto, Uint8Array,
        confirm:()=>false,
        sessionStorage:{getItem:k=>session.get(k)||null,setItem:(k,v)=>session.set(k,v)},
        document: {getElementById:get, createElement:tag=>new Element(tag), querySelectorAll:()=>[], querySelector:selector=>get(selector), body:new Element('body')},
        localStorage: {
            getItem:key=>{if(brokenStorage) throw Error('storage disabled'); return storage.get(key) ?? null;},
            setItem:(key,value)=>{if(brokenStorage) throw Error('storage disabled'); storage.set(key,value);},
            get length(){if(brokenStorage) throw Error('storage disabled'); return storage.size;},
            key:index=>Array.from(storage.keys())[index] ?? null,
            removeItem:key=>storage.delete(key),
        },
        addEventListener:(name,callback)=>{if(!events.has(name)) events.set(name,[]); events.get(name).push(callback);},
        navigator: {clipboard:{writeText:async text=>{context.copied=text;}}},
        location:{origin:'https://shop.example',pathname:'/',search:'',hash:''}, history:{replaceState(...args){context.historyArgs=args;}},
        URLSearchParams, Map, Number, JSON, Promise, console,
        setTimeout:()=>1, clearTimeout(){}, setInterval:()=>2, clearInterval(){}, prompt(){},
        fetch:async()=>{throw Error('unexpected fetch');},
    };
    context.window = context;
    vm.createContext(context); vm.runInContext(script, context);
    return {c:context, get, storage, session, rendered, emit:(name,event={})=>(events.get(name)||[]).forEach(fn=>fn(event)), run:code=>vm.runInContext(code, context)};
}

async function main() {
    let tests=0;
    {
        const {run}=setup(true);
        assert.equal(run('activeOrders.length'),0); tests++;
    }
    {
        const {c,run}=setup(); let calls=0,resolve;
        c.fetch=()=>{calls++; return new Promise(r=>resolve=r);};
        run('rememberOrder({id:1,access_token:"fixture-token"})');
        const first=run('orderPost("/api/check-recharge",1)');
        const second=run('orderPost("/api/check-recharge",1)');
        assert.equal(calls,1);
        resolve({json:async()=>({code:0,data:{processing:true}})});
        assert.equal((await first).code,0); await second;
        assert.equal(run('pendingActions.size'),0); tests++;
    }
    {
        const {c,get,run}=setup();
        const malicious='" onclick="globalThis.pwned=true"><img src=x onerror=alert(1)>';
        run('rememberOrder({id:1,access_token:"fixture-token"})');
        c.fetch=async()=>({ok:true,json:async()=>({code:0,data:{id:1,product_name:malicious,order_no:malicious,sell_price:13,status_text:malicious,paid:true,upstream_order_id:7001}})});
        await run('loadOrders()');
        const box=get('orderList').children[0];
        assert.equal(box.children[0].textContent.trim(),malicious);
        const copy=box.children.find(node=>node.textContent==='复制全部信息');
        copy.onclick();
        assert.ok(c.copied.includes(malicious));
        assert.equal(c.pwned,undefined);
        assert.equal(box.innerHTML,''); tests++;
    }
    {
        const {run,get}=setup();
        run('selectedProduct={buy_rate:2,service_fee:1,profit_percent:7,quotes:[{amount:10,sell_price:23}]}; selectedAmount=10; updatePrice()');
        assert.equal(get('sellPrice').textContent,'¥23.00');
        assert.equal(get('submitBtn').disabled,false); tests++;
    }
    {
        const {run,get}=setup();
        run('products=[{id:9,name:"美国礼品卡",buy_rate:7.123456,symbol:"USD",amounts:[100]}]; renderProducts()');
        const rendered=get('productList').innerHTML;
        assert.ok(rendered.includes('美国礼品卡'));
        assert.ok(!/汇率|元\/单位|7\.123456|product-price/.test(rendered)); tests++;
    }
    {
        const {run}=setup();
        for (const [flag,country] of [['🇺🇸','美国'],['🇹🇷','土耳其'],['🇬🇧','英国'],['🇨🇦','加拿大'],['🇦🇺','澳大利亚'],['🇩🇪','德国'],['🇧🇷','巴西'],['🇫🇮','芬兰'],['🇳🇱','荷兰'],['🇮🇹','意大利'],['🇮🇪','爱尔兰'],['🇪🇸','西班牙'],['🇫🇷','法国'],['🇨🇭','瑞士'],['🇯🇵','日本'],['🇦🇹','奥地利'],['🇧🇪','比利时']]) {
            const product={name:`Apple/iTunes ${flag} (${country})`,country:'代加',symbol:'€'};
            assert.equal(run('productFlag('+JSON.stringify(product)+')'),flag);
        }
        tests++;
    }
    {
        const {run}=setup();
        assert.equal(run('productFlag({name:"礼品卡",country:"日本 🇯🇵"})'),'🇯🇵');
        assert.equal(run('productFlag({name:"美国 🇺🇸",country:"英国 🇬🇧"})'),'🇺🇸');
        for (const product of [{},{name:null,country:3},{name:'通用礼品卡',country:'代加',symbol:'€'},{name:'🇺'}]) {
            assert.equal(run('productFlag('+JSON.stringify(product)+')'),'🌐');
        }
        tests++;
    }
    {
        const {run,get}=setup();
        run('products=[{id:9,name:"Apple/iTunes 🇯🇵 (日本)",country:"代加"}]; renderProducts()');
        const rendered=get('productList').innerHTML;
        assert.ok(rendered.includes('<span class="product-mark" aria-hidden="true">🇯🇵</span>'));
        assert.ok(rendered.includes('Apple/iTunes 🇯🇵 (日本)'));
        assert.ok(!rendered.includes('<svg')); tests++;
    }
    {
        const {run,get}=setup();
        const malicious={id:9,name:'<img src=x onerror=alert(1)> 🇺🇸',country:'<script>alert(1)</script>'};
        run('products=['+JSON.stringify(malicious)+']; renderProducts()');
        const rendered=get('productList').innerHTML;
        assert.ok(rendered.includes('aria-hidden="true">🇺🇸</span>'));
        assert.ok(rendered.includes('&lt;img'));
        assert.ok(!/<img|<script/.test(rendered)); tests++;
    }
    {
        const {run,get}=setup();
        assert.ok(html.includes('id="originalPrice"'));
        for (const [original,sell,display] of [[100,103,103],[100,97,100],[100,100,100],[100.25,104.25,104.25],[100.25,97.25,100.25]]) {
            run(`selectedProduct={quotes:[{amount:100,original_price:${original},sell_price:${sell}}]}; selectedAmount=100; updatePrice()`);
            assert.equal(get('originalPrice').textContent,'¥'+display.toFixed(2));
            assert.equal(get('sellPrice').textContent,'¥'+sell.toFixed(2));
        }
        tests++;
    }
    {
        const {run,get}=setup();
        // 100 face value * 0.99 rate = 99; the 1.25 fee belongs only to the pricing cost.
        for (const [sell,display] of [[97.25,99],[99,99],[100.25,100.25],[104.25,104.25]]) {
            run(`selectedProduct={buy_rate:0.99,service_fee:1.25,quotes:[{amount:100,original_price:99,sell_price:${sell}}]}; selectedAmount=100; updatePrice()`);
            assert.equal(get('originalPrice').textContent,'¥'+display.toFixed(2));
            assert.equal(get('sellPrice').textContent,'¥'+sell.toFixed(2));
        }
        tests++;
    }
    {
        const {run,get}=setup();
        for (const original of ['undefined','null','0','-1','NaN','Infinity','"100"']) {
            run(`selectedProduct={quotes:[{amount:100,original_price:${original},sell_price:103}]}; selectedAmount=100; updatePrice()`);
            assert.equal(get('originalPrice').textContent,'暂不可用');
            assert.equal(get('sellPrice').textContent,'¥103.00');
            assert.equal(get('submitBtn').disabled,false);
        }
        tests++;
    }
    {
        const {run,get}=setup();
        run('products=[{id:60,name:"Apple/iTunes 🇺🇸 (美国)",buy_rate:6.14,reference_rate:6.674,amounts:[20,25],quotes:[{amount:20,original_price:133.48,sell_price:126.80},{amount:25,original_price:166.85,sell_price:158.50}]},{id:88,name:"Apple/iTunes 🇯🇵 (日本)",amounts:[1000],quotes:[{amount:1000,original_price:42.50,sell_price:39}]}]; selectProduct(60)');
        assert.equal(get('originalPrice').textContent,'¥133.48');
        assert.equal(get('sellPrice').textContent,'¥126.80');
        run('selectedAmount=25; updatePrice()');
        assert.equal(get('originalPrice').textContent,'¥166.85');
        run('selectProduct(88)');
        assert.equal(get('originalPrice').textContent,'¥42.50'); tests++;
    }
    {
        const {run,get}=setup();
        run('products=[{id:9,name:"A",amounts:[10,20],quotes:[{amount:10,original_price:100,sell_price:97},{amount:20,original_price:200,sell_price:206}]},{id:10,name:"B",amounts:[10],quotes:[{amount:10,original_price:30,sell_price:29}]}]; selectProduct(9)');
        assert.equal(get('originalPrice').textContent,'¥100.00');
        run('selectedAmount=20; updatePrice()');
        assert.equal(get('originalPrice').textContent,'¥206.00');
        run('selectProduct(10)');
        assert.equal(get('originalPrice').textContent,'¥30.00');
        assert.equal(get('sellPrice').textContent,'¥29.00'); tests++;
    }
    {
        const {run,get}=setup();
        run('selectedProduct={quotes:[{amount:10,original_price:100,sell_price:97}]}; selectedAmount=10; updatePrice()');
        assert.equal(get('originalPrice').textContent,'¥100.00');
        run('selectedProduct={quotes:[{amount:10,original_price:100,sell_price:null}]}; updatePrice()');
        assert.equal(get('originalPrice').textContent,'—');
        assert.equal(get('priceBox').style.display,'none');
        assert.equal(get('submitBtn').disabled,true); tests++;
    }
    {
        const {run,get}=setup();
        for (const price of [102.50,104.01,130.45,97,97.25,95.25]) {
            run(`products=[{id:9,name:'fixture',amounts:[10],symbol:'USD',quotes:[{amount:10,sell_price:${price}}]}]; selectProduct(9)`);
            assert.equal(get('sellPrice').textContent,'¥'+price.toFixed(2));
            assert.equal(run('giftcardPrice(selectedProduct,10)'),price);
            assert.ok(get('amountChips').innerHTML.includes('约 ¥'+price.toFixed(2)));
        }
        tests++;
    }
    {
        const {run,get}=setup();
        for (const quote of ['{}','{quotes:[]}','{quotes:[{amount:10,sell_price:null}]}','{quotes:[{amount:10,sell_price:-1}]}','{quotes:[{amount:10,sell_price:NaN}]}']) {
            run(`selectedProduct=${quote}; selectedAmount=10; updatePrice()`);
            assert.equal(get('submitBtn').disabled,true);
            assert.equal(get('priceBox').style.display,'none');
        }
        tests++;
    }
    {
        const {c,run}=setup();
        c.location.hash='#order=12&token='+('a'.repeat(43));
        run('restoreOrderLink()');
        assert.equal(run('activeOrders[0].id'),12);
        assert.equal(c.historyArgs[2],'/');
        run('copyRecoveryLink(12)');
        assert.ok(c.copied.startsWith('https://shop.example/#order=12&token=')); tests++;
    }
    {
        const {c,get,run}=setup(); let calls=0,resolve;
        c.fetch=()=>{calls++; return new Promise(r=>resolve=r);};
        run('rememberOrder({id:1,access_token:"fixture-token"}); showPayDialog({id:1,sell_price:13,qr_code:"data:image/svg+xml;test"},"ai")');
        const first=c.retryPaymentCheck(); const second=c.retryPaymentCheck();
        assert.equal(calls,1);
        resolve({json:async()=>({code:0,data:{paid:true,processing:true,status_text:'处理中'}})});
        await first; await second;
        assert.equal(get('payStatus').textContent,'处理中');
        assert.notEqual(get('payDialog').removed,true); tests++;
    }
    {
        const {c,run}=setup();
        c.fetch=async()=>({json:async()=>({code:0,data:{paid:true,need_token:true,order_id:1,provider:'claude'}})});
        run('rememberOrder({id:1,access_token:"fixture-token"}); showAiTokenDialog=(id,provider)=>{window.gotProvider=provider;}; showPayDialog({id:1,sell_price:13},"ai")');
        await c.retryPaymentCheck();
        assert.equal(c.gotProvider,'claude'); tests++;
    }
    {
        const {run,get}=setup();
        run('plans=Array.from({length:6},(_,i)=>({id:i+1,name:"套餐"+(i+1),provider_code:i<4?"chatgpt":"claude",provider_name:i<4?"ChatGPT":"Claude",subtitle:"测试",available_stock:i<2?1:0,sell_price:100})); renderPlans()');
        const rendered=get('planList').innerHTML;
        assert.equal((rendered.match(/class="plan-item"/g)||[]).length,6);
        assert.equal((rendered.match(/href="#icon-chatgpt"/g)||[]).length,4);
        assert.equal((rendered.match(/href="#icon-claude"/g)||[]).length,2);
        assert.equal((rendered.match(/disabled aria-disabled="true"/g)||[]).length,0);
        assert.ok(!/补货|缺货|plan-stock/.test(rendered));
        assert.equal((rendered.match(/class="plan-price"/g)||[]).length,6); tests++;
    }
    {
        const {run,get}=setup();
        run('plans=[{id:1,name:"Plus",provider_code:"chatgpt",available_stock:0,sell_price:149},{id:5,name:"Claude Pro",provider_code:"claude",available_stock:0,sell_price:140}]; selectedPlan=null');
        get('aiSubmitBtn').disabled=true;
        run('selectPlan(999)');
        assert.equal(run('selectedPlan'),null);
        assert.equal(get('aiSubmitBtn').disabled,true);
        run('selectPlan(1)');
        assert.equal(run('selectedPlan.id'),1);
        assert.equal(get('aiSellPrice').textContent,'¥149.00');
        assert.equal(get('aiSubmitBtn').disabled,false);
        run('selectPlan(5)');
        assert.equal(run('selectedPlan.id'),5);
        assert.equal(get('aiSellPrice').textContent,'¥140.00');
        assert.equal(get('aiSubmitBtn').disabled,false); tests++;
    }
    {
        const {c,run,get}=setup(); const calls=[]; let resolve;
        c.fetch=(url,options)=>{calls.push({url,body:JSON.parse(options.body)});return new Promise(r=>resolve=r);};
        run('selectedPlan={id:5,provider_code:"claude",available_stock:0,sell_price:140}');
        get('aiSubmitBtn').disabled=false;
        const first=get('aiSubmitBtn').onclick(), second=get('aiSubmitBtn').onclick();
        assert.equal(calls.length,1);
        assert.equal(calls[0].url,'/api/create-ai-order');
        assert.equal(calls[0].body.plan_id,5);
        assert.equal(get('aiSubmitBtn').disabled,true);
        resolve({json:async()=>({code:1,msg:'fixture response'})});
        await first; await second;
        assert.equal(get('aiSubmitBtn').disabled,false);
        assert.equal(get('aiSubmitBtn').textContent,'立即代充'); tests++;
    }
    {
        const {run,get}=setup();
        let firstRendering;
        for (const stock of [0,1]) {
            run(`plans=Array.from({length:6},(_,i)=>({id:i+1,name:'套餐'+(i+1),provider_code:i<4?'chatgpt':'claude',provider_name:i<4?'ChatGPT':'Claude',subtitle:'',available_stock:${stock},sell_price:100})); renderPlans()`);
            const rendered=get('planList').innerHTML;
            assert.equal((rendered.match(/class="plan-group"/g)||[]).length,0);
            assert.equal((rendered.match(/class="plan-item"/g)||[]).length,6);
            assert.equal((rendered.match(/href="#icon-chatgpt"/g)||[]).length,4);
            assert.equal((rendered.match(/href="#icon-claude"/g)||[]).length,2);
            assert.equal((rendered.match(/class="plan-price"/g)||[]).length,6);
            assert.ok(!/补货|缺货|plan-stock/.test(rendered));
            assert.equal((rendered.match(/disabled aria-disabled="true"/g)||[]).length,0);
            if (firstRendering) assert.equal(rendered,firstRendering);
            else firstRendering=rendered;
        }
        assert.ok(!html.includes('.plan-group+.plan-group'));
        run('plans[4].provider_name="<img src=x onerror=alert(1)>"; renderPlans()');
        assert.ok(get('planList').innerHTML.includes('&lt;img'));
        assert.ok(!get('planList').innerHTML.includes('<img')); tests++;
    }
    {
        const {c,run,get}=setup();
        assert.ok(!/AI\s*会员/.test(html));
        assert.ok(html.includes('选择 AI 代充'));
        assert.ok(html.includes('确认代充信息'));
        assert.ok(html.includes('请先选择代充套餐'));
        assert.ok(html.includes('02 / AI 代充'));
        assert.ok(html.includes('disabled>立即代充</button>'));
        run('selectedPlan={id:1,provider_code:"chatgpt",available_stock:1}');
        c.fetch=async()=>({json:async()=>({code:1,msg:'fixture error'})});
        get('aiSubmitBtn').disabled=false;
        await get('aiSubmitBtn').onclick();
        assert.equal(get('aiSubmitBtn').textContent,'立即代充'); tests++;
    }
    {
        const shared=new Map(), a=setup(false,shared), b=setup(false,shared);
        a.run('rememberOrder({id:1,access_token:"token-A"})');
        b.run('rememberOrder({id:2,access_token:"token-B"})');
        const reloaded=setup(false,shared);
        assert.deepEqual(JSON.parse(reloaded.run('JSON.stringify(activeOrders.map(o=>o.id).sort())')),[1,2]);
        assert.equal(JSON.parse(shared.get('ai-energy-hub:order:1')).access_token,'token-A');
        assert.equal(JSON.parse(shared.get('ai-energy-hub:order:2')).access_token,'token-B');
        assert.equal(shared.has('activeOrders'),false); tests++;
    }
    {
        const shared=new Map(), a=setup(false,shared), b=setup(false,shared);
        const write=a.c.localStorage.setItem;
        a.c.localStorage.setItem=(key,value)=>{
            if(key==='ai-energy-hub:order:1') b.run('rememberOrder({id:2,access_token:"token-B"})');
            write(key,value);
        };
        a.run('rememberOrder({id:1,access_token:"token-A"})');
        const reloaded=setup(false,shared);
        assert.equal(reloaded.run('activeOrders.length'),2);
        assert.deepEqual(JSON.parse(reloaded.run('JSON.stringify(activeOrders.map(o=>o.id).sort())')),[1,2]); tests++;
    }
    {
        const shared=new Map(), a=setup(false,shared), b=setup(false,shared);
        a.run('rememberOrder({id:1,access_token:"token-A"})');
        b.emit('storage',{key:'ai-energy-hub:order:1'});
        assert.equal(b.run('activeOrders[0].access_token'),'token-A');
        a.run('rememberOrder({id:2,access_token:"token-B"})');
        b.emit('focus');
        assert.equal(b.run('activeOrders.length'),2);
        let body;
        b.c.fetch=async(url,options)=>{body=JSON.parse(options.body);return {json:async()=>({code:0,data:{}})};};
        await b.run('orderPost("/api/check-recharge",2)');
        assert.equal(body.access_token,'token-B'); tests++;
    }
    {
        const legacy=JSON.stringify([{id:1,access_token:'legacy-A'},{id:2,access_token:'legacy-B'}]);
        const shared=new Map([['activeOrders',legacy]]);
        const a=setup(false,shared);
        assert.equal(a.run('activeOrders.length'),2);
        assert.equal(shared.get('activeOrders'),legacy);
        // A still-open old version overwrites its old shared array; backups survive.
        shared.set('activeOrders',JSON.stringify([{id:3,access_token:'legacy-C'}]));
        const reloaded=setup(false,shared);
        assert.deepEqual(JSON.parse(reloaded.run('JSON.stringify(activeOrders.map(o=>o.id).sort())')),[1,2,3]); tests++;
    }
    {
        class MigratingMap extends Map {
            set(key,value) {
                if(key==='ai-energy-hub:legacy-order:1') super.set('ai-energy-hub:order:1',JSON.stringify({id:1,access_token:'new-token',saved_at:99999}));
                return super.set(key,value);
            }
        }
        const shared=new MigratingMap([['activeOrders',JSON.stringify([{id:1,access_token:'old-token'}])]]);
        const {run}=setup(false,shared);
        assert.equal(run('activeOrders[0].access_token'),'new-token');
        assert.equal(JSON.parse(shared.get('ai-energy-hub:order:1')).access_token,'new-token'); tests++;
    }
    {
        const shared=new Map([
            ['activeOrders','{bad-json'],
            ['ai-energy-hub:order:1',JSON.stringify({id:1,access_token:'good-token'})],
            ['ai-energy-hub:order:2','{bad-json'],
            ['ai-energy-hub:order:3',JSON.stringify({id:99,access_token:'wrong-key'})],
            ['ai-energy-hub:order:4',JSON.stringify({id:4,access_token:{invalid:true}})],
            ['unrelated-key','unrelated-value'],
        ]);
        const {run}=setup(false,shared);
        assert.equal(run('activeOrders.length'),1);
        assert.equal(run('activeOrders[0].id'),1);
        assert.equal(shared.get('unrelated-key'),'unrelated-value'); tests++;
    }
    {
        const {run,c,storage}=setup();
        c.localStorage.setItem=()=>{throw Error('fixture quota exhausted');};
        run('rememberOrder({id:7,access_token:"memory-token"})');
        assert.equal(run('activeOrders[0].access_token'),'memory-token');
        run('copyRecoveryLink(7)');
        assert.ok(c.copied.includes('token=memory-token'));
        assert.equal(storage.size,0); tests++;
    }
    {
        const shared=new Map(), {run}=setup(false,shared);
        for(let id=1;id<=120;id++) run(`Date.now=()=>${1000+id}; rememberOrder({id:${id},access_token:'token-${id}'})`);
        assert.equal(run('activeOrders.length'),100);
        assert.equal(shared.has('ai-energy-hub:order:1'),true);
        assert.equal(shared.has('ai-energy-hub:order:120'),true);
        run('Date.now=()=>10000; rememberOrder({id:1,access_token:"token-1"})');
        assert.equal(run('activeOrders[0].id'),1);
        const reloaded=setup(false,shared);
        assert.equal(reloaded.run('activeOrders[0].id'),1); tests++;
    }
    {
        class ReadOnlyMap extends Map {
            set(key,value) { if(key.startsWith('ai-energy-hub:')) throw Error('fixture storage full'); return super.set(key,value); }
        }
        const legacy=JSON.stringify([{id:1,access_token:'legacy-token'}]);
        const shared=new ReadOnlyMap([['activeOrders',legacy]]);
        assert.equal(setup(false,shared).run('activeOrders[0].access_token'),'legacy-token');
        assert.equal(shared.get('activeOrders'),legacy);
        assert.equal(setup(false,shared).run('activeOrders[0].access_token'),'legacy-token'); tests++;
    }
    {
        const {run,storage}=setup();
        run('rememberOrder({id:1,access_token:"valid-token"})');
        const before=storage.get('ai-energy-hub:order:1');
        run('rememberOrder({id:1,access_token:{invalid:true}}); rememberOrder({id:-1,access_token:"bad"})');
        assert.equal(storage.get('ai-energy-hub:order:1'),before);
        assert.equal(storage.size,1); tests++;
    }
    {
        const {c,run,session}=setup(); const keys=[];
        c.fetch=async(_url,opts)=>{keys.push(opts.headers['Idempotency-Key']); if(keys.length===1) throw Error('response lost'); return {json:async()=>({code:0,data:{id:5,access_token:'fixture-token'}})};};
        await assert.rejects(run("checkoutPost('/api/create-order',{product_id:9,amount:10,password:'private-fixture'})"));
        await run("checkoutPost('/api/create-order',{product_id:9,amount:10,password:'private-fixture'})");
        assert.equal(keys[0],keys[1]); assert.match(keys[0],/^[0-9a-f]{64}$/);
        assert.ok(!JSON.stringify([...session.values()]).includes('private-fixture')); tests++;
    }
    {
        const {c,run,session}=setup(); let calls=0,finish;
        c.fetch=()=>{calls++; return new Promise(r=>{finish=r;});};
        const first=run("checkoutPost('/api/create-ai-order',{plan_id:9})"), second=run("checkoutPost('/api/create-ai-order',{plan_id:9})");
        assert.equal(calls,1); finish({json:async()=>({code:0,data:{id:6,access_token:'fixture-token'}})});
        await Promise.all([first,second]); assert.ok(JSON.parse([...session.values()][0]).completed); tests++;
    }
    {
        const a=setup(); let first;
        a.c.fetch=async(_url,opts)=>{first=opts.headers['Idempotency-Key']; throw Error('response lost');};
        await assert.rejects(a.run("checkoutPost('/api/create-order',{product_id:9,amount:10})"));
        const b=setup(); b.c.sessionStorage=a.c.sessionStorage; let recovered;
        b.c.fetch=async(_url,opts)=>{recovered=opts.headers['Idempotency-Key']; return {json:async()=>({code:0,data:{id:8,access_token:'fixture-token'}})};};
        await b.run("checkoutPost('/api/create-order',{product_id:9,amount:10})");
        assert.equal(first,recovered); tests++;
    }
    {
        const {c,run}=setup(); const keys=[];
        c.fetch=async(_url,opts)=>{keys.push(opts.headers['Idempotency-Key']); return {json:async()=>({code:0,data:{id:9,access_token:'fixture-token'}})};};
        await run("checkoutPost('/api/create-order',{product_id:9,amount:10})");
        await run("checkoutPost('/api/create-order',{product_id:9,amount:10})");
        assert.equal(keys[0],keys[1]); c.confirm=()=>true;
        await run("checkoutPost('/api/create-order',{product_id:9,amount:10})");
        assert.notEqual(keys[1],keys[2]); tests++;
    }
    {
        const {c,run}=setup(); const keys=[];
        c.sessionStorage.setItem=()=>{throw Error('quota');};
        c.fetch=async(_url,opts)=>{keys.push(opts.headers['Idempotency-Key']);return {json:async()=>({code:409,msg:'pending'})};};
        await run("checkoutPost('/api/create-ai-order',{plan_id:9})");
        await run("checkoutPost('/api/create-ai-order',{plan_id:9})");
        assert.equal(keys[0],keys[1]); tests++;
    }
    {
        const {c,run}=setup(); const keys=[];
        c.fetch=async(_url,opts)=>{keys.push(opts.headers['Idempotency-Key']);return {json:async()=>({code:500})};};
        await run("checkoutPost('/api/create-order',{product_id:9,amount:10})");
        await run("checkoutPost('/api/create-order',{product_id:9,amount:20})");
        assert.notEqual(keys[0],keys[1]); tests++;
    }
    {
        const {run}=setup();
        run("let paidOrdersLoaded=0; loadOrders=()=>{paidOrdersLoaded++;}; showPayDialog({paid:true,id:1},'giftcard')");
        assert.equal(run('paidOrdersLoaded'),1); assert.ok(!run('window.payContext')); tests++;
    }
    for (const provider of ['chatgpt', 'claude']) {
        const {run, rendered} = setup();
        run(`showAiTokenDialog(7, '${provider}')`);
        const markup = rendered[rendered.length - 1];
        assert.match(markup, /class="credential-modal-header"/);
        assert.match(markup, /role="dialog" aria-modal="true" aria-labelledby="aiTokenDialogTitle"/);
        assert.match(markup, /<button type="button" class="modal-dismiss" aria-label="关闭账号信息弹窗"[^>]*onclick="closeAiTokenDialog\(\)"/);
        assert.ok(markup.includes(provider === 'claude' ? 'id="aiOrgConfirm"' : 'id="aiTokenInput"'));
        assert.ok(markup.includes('onclick="submitAiToken(7)"'));
        tests++;
    }
    {
        const {c, run, get, storage} = setup();
        let requests = 0;
        c.fetch = async () => { requests++; throw Error('closing must not submit'); };
        run('rememberOrder({id:7,access_token:"fixture-token"}); showAiTokenDialog(7)');
        const orders = run('JSON.stringify(activeOrders)'), saved = JSON.stringify([...storage]);
        run('closeAiTokenDialog(); closeAiTokenDialog()');
        assert.equal(get('aiTokenDialog').removed, true);
        assert.equal(run('JSON.stringify(activeOrders)'), orders);
        assert.equal(JSON.stringify([...storage]), saved);
        assert.equal(requests, 0);
        tests++;
    }
    {
        const {c, run, get, storage, session} = setup();
        const value = JSON.stringify({accessToken:'fixture-session-secret-' + 'x'.repeat(70000), user:{name:'测试账号'}});
        c.document.getElementById = id => id === 'aiOrgInput' ? null : get(id);
        run('rememberOrder({id:7,access_token:"fixture-order-token"}); showAiTokenDialog(7); loadOrders=()=>{}');
        get('aiTokenInput').value = value;
        const requests = [];
        c.fetch = async (url, options) => {
            requests.push({url, body:JSON.parse(options.body)});
            return {json:async()=>({code:0,data:{status_text:'充值处理中'}})};
        };
        await run('submitAiToken(7)');
        assert.equal(requests.length, 1);
        assert.equal(requests[0].url, '/api/submit-ai-token');
        assert.equal(typeof requests[0].body.token, 'string');
        assert.equal(requests[0].body.token, value);
        assert.equal(requests[0].body.access_token, 'fixture-order-token');
        assert.equal(get('aiTokenDialog').removed, true);
        assert.ok(!JSON.stringify([...storage, ...session]).includes('fixture-session-secret-'));
        tests++;
    }
    {
        const {c, run, get, storage, session} = setup();
        const value = JSON.stringify({accessToken:'fixture-session-secret-' + 'x'.repeat(70000)});
        run('selectedPlan={id:9,provider_code:"chatgpt"}; showPayDialog=()=>{}');
        get('aiToken').value = value;
        let body;
        c.fetch = async (url, options) => {
            assert.equal(url, '/api/create-ai-order');
            body = JSON.parse(options.body);
            return {json:async()=>({code:0,data:{id:7,access_token:'fixture-order-token'}})};
        };
        await get('aiSubmitBtn').onclick();
        assert.equal(typeof body.ai_token, 'string');
        assert.equal(body.ai_token, value);
        assert.equal(get('aiToken').value, '');
        assert.ok(!JSON.stringify([...storage, ...session]).includes('fixture-session-secret-'));
        tests++;
    }
    {
        const {c, run, get} = setup();
        const org = '12345678-1234-1234-1234-123456789abc';
        run('rememberOrder({id:7,access_token:"fixture-order-token"}); showAiTokenDialog(7,"claude"); loadOrders=()=>{}');
        get('aiOrgInput').value = org;
        get('aiOrgConfirm').value = org;
        let body;
        c.fetch = async (_url, options) => {
            body = JSON.parse(options.body);
            return {json:async()=>({code:0,data:{status_text:'充值处理中'}})};
        };
        await run('submitAiToken(7)');
        assert.equal(typeof body.token, 'string');
        assert.deepEqual(JSON.parse(body.token), {organization_id:org,organization_id_confirm:org});
        tests++;
    }
    {
        const {c, run, get} = setup();
        c.document.getElementById = id => id === 'aiOrgInput' ? null : get(id);
        run('rememberOrder({id:7,access_token:"fixture-order-token"}); showAiTokenDialog(7)');
        get('aiTokenInput').value = '{"fixture":true}';
        const messages = [];
        c.showToast = message => messages.push(message);
        c.fetch = async () => ({json:async()=>({code:1,msg:'账号信息过长，完整 JSON 不能超过 256 KiB'})});
        await run('submitAiToken(7)');
        assert.deepEqual(messages, ['提交失败: 账号信息过长，完整 JSON 不能超过 256 KiB']);
        assert.equal(get('aiTokenDialog').removed, undefined);
        assert.equal(get('aiTokenInput').value, '{"fixture":true}');
        tests++;
    }
    console.log(`PASS frontend_tests=${tests}`);
}
main().catch(error=>{console.error(error);process.exitCode=1;});

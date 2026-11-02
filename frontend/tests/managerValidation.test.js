import test from 'node:test';
import assert from 'node:assert/strict';
import {validateManagerResponse} from '../src/managerValidation.js';
const base={direction:'accept_split',date:'',quantity:'',comment:'确认现场情况'};
test('截图中的日期非空但数量为空会直接提示数量',()=>{assert.match(validateManagerResponse({...base,date:'2026-11-05'}).quantity,/数量/)});
test('接受分批可不指定目标；指定数量则必须带日期',()=>{assert.deepEqual(validateManagerResponse(base),{});assert.match(validateManagerResponse({...base,quantity:'100'}).date,/日期/)});
test('调货和加急都要填写数量与日期',()=>{for(const direction of ['transfer','expedite']){const errors=validateManagerResponse({...base,direction});assert.ok(errors.quantity);assert.ok(errors.date);assert.deepEqual(validateManagerResponse({...base,direction,quantity:'100',date:'2026-11-05'}),{})}});
test('拒绝无效数量和空白说明',()=>{for(const quantity of ['0','201','1.5','bad'])assert.ok(validateManagerResponse({...base,quantity,date:'2026-11-05'}).quantity);assert.ok(validateManagerResponse({...base,comment:'  '}).comment)});

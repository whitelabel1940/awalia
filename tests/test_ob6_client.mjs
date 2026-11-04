import assert from 'node:assert/strict';
import {questionnaireNamed} from '../cbs/web/static/questionnaire.js';
const template={scene:'{name}の立場は？',choices:{leave_open:'{name}に任せる'},multi:false};
for(const [name,expected] of [['テスト係','テスト係'],['','あなたのパートナー'],['  ','あなたのパートナー']]){
 const q=questionnaireNamed(template,name);
 assert.equal(q.scene,`${expected}の立場は？`);assert.equal(q.choices.leave_open,`${expected}に任せる`);assert.equal(q.multi,false);
}
assert.equal(template.scene,'{name}の立場は？');
console.log('OB6: 3 name substitution cases passed');

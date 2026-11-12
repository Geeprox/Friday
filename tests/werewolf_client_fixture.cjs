const fs=require('node:fs');
const recap=fs.readFileSync('homeplane/static/werewolf-recap.js','utf8')
 .replace('export function makeWerewolfRecap','function makeWerewolfRecap');

function readClient(name) {
 const source=fs.readFileSync('homeplane/static/werewolf-'+name+'.js','utf8');
 return 'const makeWerewolfRecap=(()=>{\n'+recap+'\nreturn makeWerewolfRecap;\n})();\n'
  +source.replace("import {makeWerewolfRecap} from './werewolf-recap.js';",'');
}

module.exports={readClient};

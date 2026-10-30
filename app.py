import os,random,secrets,string,threading,time
from flask import Flask,render_template,request,jsonify
from data import MONSTERS,CARDS,ABILITIES,TERRAINS,deck_for
app=Flask(__name__); rooms={}; lock=threading.RLock()

def validate_deck(team, supplied, roster=None):
 """Validate the complete 21-card deck on the server, never trust the browser."""
 if not isinstance(supplied, list) or len(supplied)!=21:
  raise ValueError('Колода должна содержать ровно 21 карту')
 from collections import Counter
 if any(not isinstance(cid,str) or cid not in CARDS for cid in supplied):
  raise ValueError('В колоде обнаружена неизвестная карта')
 counts=Counter(supplied)
 if any(n>2 for n in counts.values()):
  raise ValueError('Не более двух копий одной карты')
 if any(CARDS[c]['team'] not in (team,'neutral') for c in supplied):
  raise ValueError('Нельзя добавлять карты чужой стихии')
 normal=[c for c in supplied if CARDS[c]['kind']=='ability']
 tiles=[c for c in supplied if CARDS[c]['kind']=='terrain']
 if len(normal)!=15 or len(tiles)!=6:raise ValueError('Нужно 15 способностей и 6 территорий')
 # Playtest: awakened abilities may be selected from the start; evolution trigger is not set yet.
 if any(CARDS[c]['team']=='neutral' for c in tiles):raise ValueError('Нейтральных территорий пока нет')
 n_neutral=sum(CARDS[c]['team']=='neutral' for c in normal)
 if n_neutral>3:raise ValueError('Максимум 3 физических нейтральных карты')
 if roster is None:
  # Backward compatibility for existing saved decks.
  roster=[mon for mon in MONSTERS if MONSTERS[mon]['team']==team and any(CARDS[c]['owner']==mon for c in normal)]
 if not isinstance(roster,list) or len(roster)!=3 or len(set(roster))!=3 or any(not isinstance(mon,str) or mon not in MONSTERS or MONSTERS[mon]['team']!=team for mon in roster):
  raise ValueError('Выберите ровно 3 разных монстров своей стихии')
 if any(CARDS[c]['owner'] not in roster for c in normal if CARDS[c]['team']!='neutral'):
  raise ValueError('В колоде есть способности монстров вне выбранной команды')
 for mon in roster:
  selected=[(cid,n) for cid,n in counts.items() if CARDS[cid]['kind']=='ability' and CARDS[cid]['owner']==mon]
  if len(selected)!=3 or not 3<=sum(n for _,n in selected)<=5:
   raise ValueError('Для '+MONSTERS[mon]['name']+' нужны 3 разные способности и 3–5 физических карт')
 return list(supplied),list(roster)

def new_room(fire_deck=None,fire_roster=None):
 code=''.join(random.choices(string.ascii_uppercase+string.digits,k=6))
 while code in rooms:code=''.join(random.choices(string.ascii_uppercase+string.digits,k=6))
 tokens=[secrets.token_urlsafe(20) for _ in range(2)]
 p=[]
 for team in ('fire','air'):
  if team=='fire' and fire_deck is not None:deck,roster=validate_deck('fire',fire_deck,fire_roster)
  else:
   deck=deck_for(team);roster=list({cid.split('_a')[0] for cid in deck if '_a' in cid and not cid.startswith('neutral_')})
  abilities=[x for x in deck if CARDS[x]['kind']=='ability']; territories=[x for x in deck if CARDS[x]['kind']=='terrain']; random.shuffle(abilities);random.shuffle(territories)
  hand=abilities[:3]+territories[:1]; deck=abilities[3:]+territories[1:];random.shuffle(deck)
  p.append(dict(team=team,monsters=roster,token=tokens[len(p)],joined=len(p)==0,hand=hand,deck=deck,discard=[],pending=[],mulligan=False,score=0))
 active_roster=set(p[0]['monsters']+p[1]['monsters'])
 mons={k:dict(cp=v['hp'],max=v['hp'],attack=v['atk'],slot=None,dead=k not in active_roster,inactive=k not in active_roster,burn=0,shield=0,shield_hits=0,bonus=0,buff_hits=0,buff_expire=0,notes='',first_entry_round=-1,awakened=False) for k,v in MONSTERS.items()}
 rooms[code]=dict(code=code,players=p,turn=0,round=1,ap=2,first_turn=[True,True],tiles=[None]*6,monsters=mons,log=['Создана комната '+code],winner=None,reaction=None,turn_serial=0,history=[])
 return rooms[code]

def actor(r,t):
 for i,p in enumerate(r['players']):
  if p['token']==t and p['joined']: return i
 return None

def error(msg): return jsonify(error=msg),400

def log(r,s):r['log'].insert(0,s);r['log']=r['log'][:120]
def owner(m):return 0 if MONSTERS[m]['team']=='fire' else 1

def cleanup(r):
 for slot in range(6):
  mons=[m for m,s in r['monsters'].items() if s['slot']==slot and not s['dead']]
  if not mons:continue
  teams={owner(m) for m in mons}
  # Only finish if a collision has been explicitly marked active in the tile.
  tile=r['tiles'][slot]
  if tile and tile.get('clash') and len(teams)<2:
   for m in mons:leave(r,m)
   card=tile['card'];r['players'][tile['owner']]['discard'].append(card)
   r['tiles'][slot]=None;log(r,f'Столкновение завершено в слоте {slot+1}. Победители вернулись в резерв, территория сброшена.')

def apply_burn(r, target, amount):
 s=r['monsters'][target]
 if s['dead']:return
 tile=r['tiles'][s['slot']] if s['slot'] is not None else None
 if tile and tile.get('furnace_until',-1)>=r['round'] and owner(target)!=tile.get('furnace_owner'):
  amount*=2
 before=s['burn'];s['burn']=min(5,before+amount)
 if s['burn']!=before:log(r,f'Горение {MONSTERS[target]["name"]}: {before} → {s["burn"]}')
 caldra=r['monsters'].get('kaldra')
 if caldra and not caldra['dead'] and caldra['slot'] is not None and s['slot']==caldra['slot'] and owner(target)!=0 and s['burn']>before and r.get('kaldra_proc_round')!=r['round']:
  r['kaldra_proc_round']=r['round'];hit(r,target,5);log(r,'Горнило Кальдры: дополнительно 5 урона')
 solar=r['monsters'].get('solaris')
 if amount>0 and owner(target)==1 and solar and not solar['dead'] and r.get('solar_healed_round')!=r['round']:
  allies=[m for m,v in r['monsters'].items() if owner(m)==0 and v['slot'] is not None and not v['dead']]
  if allies:
   a=allies[0];r['monsters'][a]['cp']=min(r['monsters'][a]['max'],r['monsters'][a]['cp']+10);r['solar_healed_round']=r['round']
   log(r,f'Тёплое пламя: {MONSTERS[a]["name"]} восстанавливает 10 CP')

def slot_bonus(r,m):
 slot=r['monsters'][m]['slot']
 if slot is None:return 0
 tile=r['tiles'][slot]
 return 10 if tile and tile['card']=='fire_t0' and MONSTERS[m]['team']=='fire' and r['monsters'][m].get('no_terrain_buff_until',-1)<r['round'] else 0

def enter(r,m,slot,forced=False,initiator=None):
 s=r['monsters'][m];s['slot']=slot
 tile=r['tiles'][slot]
 if tile and tile['card']=='fire_t2' and MONSTERS[m]['team']=='fire':
  s['max']+=20;s['cp']+=20;s['heart']=True
 if tile and tile['card']=='fire_t1' and owner(m)!=tile['owner']:apply_burn(r,m,1)
 if tile and tile['card']=='fire_t4':apply_burn(r,m,2)
 if tile and tile.get('hot_until',-1)>=r['round'] and tile.get('hot_owner')!=owner(m):apply_burn(r,m,1)
 if tile and tile.get('lava_next',0)>0 and tile.get('lava_owner')!=owner(m):
  amount=tile.pop('lava_next');apply_burn(r,m,amount)
 enemies=[e for e,t in r['monsters'].items() if t['slot']==slot and owner(e)!=owner(m) and not t['dead']]
 if enemies:
  if tile:tile['clash']=True
  attacker=initiator or m
  if attacker==m and m=='ignis' and s['first_entry_round']!=r['round']:
   already=r['monsters'][enemies[0]]['burn']>0
   apply_burn(r,enemies[0],1)
   if already:hit(r,enemies[0],10)
   s['first_entry_round']=r['round']
  if m=='lavrix' and s.get('first_entry_round')!=r['round']:
   apply_burn(r,enemies[0],1);s['first_entry_round']=r['round']
  if m!='magmor' and 'magmor' in r['monsters'] and r['monsters']['magmor']['slot']==slot and owner(m)!=0 and r.get('magmor_proc_round')!=r['round']:
   apply_burn(r,m,1);r['magmor_proc_round']=r['round']
  target=m if initiator and owner(initiator)!=owner(m) else enemies[0]
  attacker=initiator or m
  if r['monsters'][target]['dead']:return
  damage=max(0,r['monsters'][attacker]['attack']+r['monsters'][attacker]['bonus']+slot_bonus(r,attacker)-r['monsters'][attacker].get('weaken',0))
  q=dict(type='damage',player=owner(target),target=target,attacker=attacker,source='basic',amount=damage,deadline=time.time()+10)
  if available_reactions(r,owner(target),q):
   r['reaction']=q
   log(r,f'Инициатива: {MONSTERS[attacker]["name"]} атакует {MONSTERS[target]["name"]}. Окно реакции 10 секунд')
  else:
   done=hit(r,target,damage)
   if r['monsters'][attacker].pop('first_attack_burn',False) and not r['monsters'][target]['dead']:apply_burn(r,target,1)
   log(r,f'Инициатива: {MONSTERS[attacker]["name"]} атакует {MONSTERS[target]["name"]} на {done} CP')
  consume_buff(r,attacker)

def leave(r,m):
 s=r['monsters'][m]
 if s['slot'] is None:return
 partner=s.pop('linked_partner',None)
 if partner:
  other=r['monsters'][partner]
  other.pop('linked_partner',None)
  if other.get('linked_until_leave'):
   other['linked_until_leave']=False;other['bonus']=0
  if s.get('linked_until_leave'):
   s['linked_until_leave']=False;s['bonus']=0
 if s.get('heart'):
  s['heart']=False;s['max']-=20;s['cp']-=20
  if s['cp']<=0:hit(r,m,0)
 s['slot']=None

def consume_buff(r,m):
 s=r['monsters'][m]
 if s['buff_hits']>0:
  s['buff_hits']-=1
  if not s['buff_hits']:s['bonus']=0

def hit(r,target,n,min_hp=0):
 s=r['monsters'][target]
 if s['dead']:return 0
 reduction=s['shield'] if s.get('shield_hits',0)>0 else 0
 actual=max(0,n-max(0,reduction))
 if target=='boreal' and r['turn']!=owner(target) and s.get('boreal_defense_serial')!=r['turn_serial'] and actual>0:
  actual=max(0,actual-10);s['boreal_defense_serial']=r['turn_serial']
 if min_hp:actual=min(actual,max(0,s['cp']-min_hp))
 if s.get('shield_hits',0)>0:
  s['shield_hits']-=1
  if s['shield_hits']==0:s['shield']=0
 s['cp']-=actual
 if s['cp']<=0:
  s['dead']=True;s['slot']=None;r['players'][1-owner(target)]['score']+=1
  log(r,f'{MONSTERS[target]["name"]} уничтожен. +1 очко противнику.')
  # Remove ability cards of dead monster from all deck zones; no deck recycling for them.
  for p in r['players']:
   for zone in ('hand','deck','discard','pending'):
    p[zone]=[x for x in p[zone] if CARDS[x]['owner']!=target]
  if r['players'][1-owner(target)]['score']>=2:r['winner']=1-owner(target);log(r,'Матч завершён: '+('Огонь' if r['winner']==0 else 'Воздух')+' победил!')
 return actual

def available_reactions(r, player, q):
 """Return only reactions legally playable by the defending player from their hand."""
 p=r['players'][player]
 target=q['target']
 s=r['monsters'][target]
 if s['dead']:return []
 on_field=lambda m: m in r['monsters'] and not r['monsters'][m]['dead'] and r['monsters'][m]['slot'] is not None
 available=[]
 for c in dict.fromkeys(p['hand']):
  if q['type'] in ('push','pull'):
   if c=='sylphida_a2' and on_field('sylphida'):available.append(c)
   elif c=='ignis_a4' and target=='ignis' and on_field('ignis'):available.append(c)
   elif c=='eylar_a2' and on_field('eylar') and owner(target)==player:available.append(c)
  elif q['type']=='damage':
   if c=='neutral_a1':available.append(c)
   elif c=='aerion_a8' and target=='aerion' and on_field('aerion'):available.append(c)
   elif c=='veyr_a8' and q.get('source')=='basic' and on_field('veyr'):available.append(c)
   elif c=='solaris_a5' and on_field('solaris') and q['amount'] >= s['cp']+s.get('shield',0):available.append(c)
   elif c=='kaldra_a2' and on_field('kaldra') and s['slot']==r['monsters']['kaldra']['slot'] and owner(target)==player:available.append(c)
   elif c=='lavrix_a2' and target=='lavrix' and on_field('lavrix'):available.append(c)
   elif c=='boreal_a1' and on_field('boreal') and s['slot']==r['monsters']['boreal']['slot'] and owner(target)==player:available.append(c)
   elif c=='boreal_a7' and on_field('boreal') and s['slot']==r['monsters']['boreal']['slot'] and q['amount']>=s['cp']+s.get('shield',0):available.append(c)
  elif q['type']=='burn':
   if c=='sylphida_a5' and on_field('sylphida'):available.append(c)
 return available

def tick_burn(r,m):
 s=r['monsters'][m]
 if s['dead'] or s['burn']<=0:return
 n=s['burn']; dealt=hit(r,m,n*5);s['burn']=max(0,s['burn']-1)
 log(r,f'Горение: {MONSTERS[m]["name"]} теряет {dealt} CP, стаки {n}→{s["burn"]}')

def finish_turn(r,i,discards,skip_burn=None):
 """Continue end-of-turn cleanup after an optional negative-effect reaction."""
 p=r['players'][i]
 for m,st in r['monsters'].items():
  if m!=skip_burn and owner(m)==i:tick_burn(r,m)
 cleanup(r)
 if r['winner'] is not None:return
 for c in discards:
  if c in p['hand']:p['hand'].remove(c);p['discard'].append(c)
 r['first_turn'][i]=False;r['turn']=1-i;r['ap']=2;r['turn_serial']+=1
 for st in r['monsters'].values():
  if st.get('weaken_expire') and r['round']>=st['weaken_expire']:
   st['weaken']=0;st['weaken_expire']=0
  if st.get('buff_expire') and r['round']>=st['buff_expire']:
   st['bonus']=0;st['buff_expire']=0
 if i==1:r['round']+=1
 log(r,f'Ход игрока {i+1} завершён')

def resolve_reaction(r, cancel=False, card=None):
 q=r.get('reaction')
 if not q:return
 r['reaction']=None
 if q['type']=='damage':
  amount=q['amount']; min_hp=0
  if card in ('neutral_a1','aerion_a8','kaldra_a2','boreal_a1'):amount=max(0,amount-20)
  elif card=='lavrix_a2':amount=max(0,amount-15)
  elif card=='veyr_a8':amount=0
  elif card in ('solaris_a5','boreal_a7'):min_hp=1
  hit(r,q['target'],amount,min_hp=min_hp)
  if card=='lavrix_a2' and q.get('attacker') in r['monsters'] and not r['monsters'][q['attacker']]['dead']:apply_burn(r,q['attacker'],1)
  log(r,f'Атака по {MONSTERS[q["target"]]["name"]}: {amount} урона до защиты'+(' (с реакцией)' if card else ''))
  cleanup(r)
  return
 if q['type']=='burn':
  if card=='sylphida_a5':
   r['monsters'][q['target']]['burn']=0
   log(r,f'Последний порыв: {MONSTERS[q["target"]]["name"]} очищен от горения')
  else:tick_burn(r,q['target'])
  finish_turn(r,q['player'],q.get('discard',[]),skip_burn=q['target'])
  return
 if cancel:
  if card=='eylar_a2':r['monsters'][q['target']]['burn']=max(0,r['monsters'][q['target']]['burn']-1)
  if card=='ignis_a4':
   slot=r['monsters']['ignis']['slot']
   if slot is not None:
    enemies=[m for m,s in r['monsters'].items() if owner(m)!=0 and s['slot']==slot and not s['dead']]
    if enemies:apply_burn(r,enemies[0],1)
  log(r,'Принудительное перемещение отменено реакцией')
  return
 m=q['target'];dest=q['dest'];initiator=q['initiator']
 if r['monsters'][m]['dead'] or r['monsters'][initiator]['dead']:return
 if q['type']=='push':
  leave(r,m);log(r,f'{MONSTERS[m]["name"]} отправлен в резерв')
 else:
  # A monster initiating a collision must be physically on the destination tile.
  # If a card moves the enemy to another tile, a friendly occupant there initiates.
  acting=initiator if r['monsters'][initiator]['slot']==dest else next((e for e,st in r['monsters'].items() if owner(e)==owner(initiator) and st['slot']==dest and not st['dead']),None)
  leave(r,m);enter(r,m,dest,forced=True,initiator=acting)
  log(r,f'Принудительный вызов: {MONSTERS[m]["name"]} в слот {dest+1}')
 cleanup(r)

def card_move(r,m,dest):
 """Apply a position-changing card and the once-per-round Eylar passive."""
 leave(r,m);enter(r,m,dest)
 eylar=r['monsters'].get('eylar')
 if eylar and not eylar['dead'] and eylar['slot'] is not None and owner(m)==1 and r.get('eylar_heal_round')!=r['round'] and not r['monsters'][m]['dead']:
  subject=r['monsters'][m];subject['cp']=min(subject['max'],subject['cp']+10);r['eylar_heal_round']=r['round']
  log(r,'Шелест жизненных троп: '+MONSTERS[m]['name']+' восстанавливает 10 CP')

def awaken_skill(r,i,m,idx,target,dest):
 """Handle new-monster playtest cards. Return an error message or None."""
 s=r['monsters'][m]
 def ally():return target in r['monsters'] and owner(target)==i and not r['monsters'][target]['dead']
 def foe():return target in r['monsters'] and owner(target)!=i and not r['monsters'][target]['dead']
 def on_tile():return foe() and s['slot'] is not None and r['monsters'][target]['slot']==s['slot']
 def valid_dest():return isinstance(dest,int) and 0<=dest<6 and r['tiles'][dest] is not None
 def heal(mid,n):
  st=r['monsters'][mid];st['cp']=min(st['max'],st['cp']+n)
 def attack_target(mid,extra=0):
  hit(r,mid,max(0,s['attack']+s.get('bonus',0)+slot_bonus(r,m)+extra-s.get('weaken',0)));consume_buff(r,m)
  if s.pop('first_attack_burn',False) and not r['monsters'][mid]['dead']:apply_burn(r,mid,1)
 if m=='kaldra':
  if idx in (0,3,5):
   if not on_tile():return 'Выберите врага на территории Кальдры'
   if idx==0:hit(r,target,15);apply_burn(r,target,1)
   if idx==3:apply_burn(r,target,2)
   if idx==5:
    if r['monsters'][target]['burn']<=0:return 'Цель должна гореть'
    hit(r,target,10+5*r['monsters'][target]['burn'])
  elif idx in (1,7):
   if not foe():return 'Выберите вражеского монстра'
   if idx==1 and (r['monsters'][target]['slot'] is None or r['monsters'][target]['slot']==s['slot']):return 'Выберите врага с другой территории'
   if idx==7 and r['monsters'][target]['slot'] is not None:return 'Выберите врага в резерве'
   return queue_force(r,i,m,target,s['slot'])
  elif idx==4:
   if s['slot'] is None or not r['tiles'][s['slot']] or r['tiles'][s['slot']]['owner']!=i:return 'Кальдра должна стоять на собственной территории'
   where=s['slot'];tile=r['tiles'][where]
   r['players'][tile['owner']]['discard'].append(tile['card']);r['tiles'][where]=None
   for mid,st in r['monsters'].items():
    if st['slot']==where and st.get('heart'):
     st['heart']=False;st['max']-=20;st['cp']-=20
     if st['cp']<=0:hit(r,mid,0)
   for enemy,st in r['monsters'].items():
    if owner(enemy)!=i and st['slot']==where and not st['dead']:hit(r,enemy,15)
  elif idx==6:
   if not ally() or r['monsters'][target]['slot']!=s['slot'] or s['slot'] is None:return 'Выберите союзника на территории Кальдры'
   heal(target,25);r['monsters'][target]['burn']=max(0,r['monsters'][target]['burn']-1)
  elif idx==8:
   if not ally():return 'Выберите союзника'
   a=r['monsters'][target];a['bonus']=10;a['buff_hits']=2;a['first_attack_burn']=True
  elif idx==9:
   if not valid_dest() or r['tiles'][dest]['owner']!=i:return 'Выберите свою территорию'
   for enemy,st in r['monsters'].items():
    if owner(enemy)!=i and st['slot']==dest and not st['dead']:hit(r,enemy,20+(10 if st['burn'] else 0))
  else:return 'Эта карта разыгрывается только как реакция'
 elif m=='lavrix':
  if idx in (0,6):
   if not valid_dest() or s['slot']==dest:return 'Выберите другую существующую территорию'
   if idx==6 and s['slot'] is not None:return 'Прыжок хищника доступен только из резерва'
   if idx==0 and s['slot'] is None:return 'Лаврикс должен находиться на поле'
   card_move(r,m,dest)
  elif idx in (1,9):
   if not on_tile():return 'Выберите противника на территории Лаврикса'
   bonus=20 if idx==9 and r['monsters'][target]['burn'] else 0
   attack_target(target,bonus)
   if idx==1:r['monsters'][target]['weaken']=10;r['monsters'][target]['weaken_expire']=r['round']+1
   elif bonus:r['monsters'][target]['burn']=max(0,r['monsters'][target]['burn']-1)
  elif idx==3:s['bonus']=15;s['buff_hits']=2
  elif idx==4:
   for enemy,st in r['monsters'].items():
    if owner(enemy)!=i and st['slot']==s['slot'] and not st['dead']:hit(r,enemy,15);apply_burn(r,enemy,1)
  elif idx==5:
   s['burn']=max(0,s['burn']-2);heal(m,20);leave(r,m)
  elif idx==7:
   if not foe() or r['monsters'][target]['slot'] is not None:return 'Выберите врага в резерве'
   return queue_force(r,i,m,target,s['slot'])
  elif idx==8:
   if not on_tile():return 'Выберите противника на территории Лаврикса'
   for enemy,st in r['monsters'].items():
    if owner(enemy)!=i and st['slot']==s['slot'] and not st['dead']:
     st['weaken']=10;st['weaken_expire']=r['round']+1
   apply_burn(r,target,1)
  else:return 'Эта карта разыгрывается только как реакция'
 elif m=='boreal':
  if idx in (0,8):
   if idx==0:
    if not on_tile():return 'Выберите противника на территории Бореала'
    attack_target(target,10)
   else:
    if not valid_dest() or s['slot'] is None or s['slot']==dest:return 'Выберите другую территорию'
    if not foe() or r['monsters'][target]['slot']!=dest:return 'Выберите врага на территории назначения'
    card_move(r,m,dest)
    if not r['monsters'][target]['dead']:hit(r,target,20)
  elif idx==2:
   if not on_tile():return 'Выберите врага на территории Бореала'
   return queue_force(r,i,m,target,s['slot'],typ='push')
  elif idx==3:s['shield']=15;s['shield_hits']=2
  elif idx==4:
   if not ally() or r['monsters'][target]['slot'] is None or not valid_dest() or r['monsters'][target]['slot']==dest:return 'Выберите союзника на поле и другую территорию'
   card_move(r,target,dest)
  elif idx==5:
   for mid,st in r['monsters'].items():
    if owner(mid)==i and st['slot']==s['slot'] and not st['dead']:st['shield']=15;st['shield_hits']=1
  elif idx==6:
   for mid,st in r['monsters'].items():
    if owner(mid)!=i and st['slot']==s['slot'] and not st['dead']:
     hit(r,mid,20);st['weaken']=10;st['weaken_expire']=r['round']+1
  elif idx==9:
   for mid,st in r['monsters'].items():
    if owner(mid)==i and st['slot']==s['slot'] and not st['dead']:st['burn']=max(0,st['burn']-2)
  else:return 'Эта карта разыгрывается только как реакция'
 elif m=='eylar':
  if idx in (0,4,9):
   if not ally():return 'Выберите живого союзника'
   st=r['monsters'][target]
   if idx==0:
    if st['slot'] is None:return 'Союзник должен находиться на поле'
    heal(target,25)
   elif idx==4:st['burn']=max(0,st['burn']-2)
   elif idx==9:st['bonus']=15;st['buff_hits']=2
  elif idx==1:
   if not ally() or r['monsters'][target]['slot'] is None or not valid_dest() or r['monsters'][target]['slot']==dest:return 'Выберите союзника и другую территорию'
   card_move(r,target,dest)
  elif idx==3:
   if not on_tile():return 'Выберите противника на территории Эйлара'
   attack_target(target)
   allies=[mid for mid,st in r['monsters'].items() if owner(mid)==i and st['slot']==s['slot'] and not st['dead']]
   if allies:heal(allies[0],10)
  elif idx==5:
   if not ally() or r['monsters'][target]['slot'] is not None:return 'Выберите союзника из резерва'
   enter(r,target,s['slot'])
  elif idx==6:
   for mid,st in r['monsters'].items():
    if owner(mid)==i and st['slot'] is not None and not st['dead']:heal(mid,15);st['burn']=max(0,st['burn']-1)
  elif idx==7:
   if not on_tile():return 'Выберите врага на территории Эйлара'
   return queue_force(r,i,m,target,s['slot'],typ='push')
  elif idx==8:
   if not foe() or r['monsters'][target]['slot'] is None:return 'Выберите врага на поле'
   hit(r,target,20);r['monsters'][target]['weaken']=10;r['monsters'][target]['weaken_expire']=r['round']+1
  else:return 'Эта карта разыгрывается только как реакция'
 return None

def queue_force(r,i,initiator,target,dest,typ='pull'):
 if target not in MONSTERS or owner(target)==i or r['monsters'][target]['dead']:return 'Выберите живого врага'
 if typ=='pull' and (not isinstance(dest,int) or not 0<=dest<6 or not r['tiles'][dest]):return 'Укажите существующую территорию'
 if typ=='pull' and r['monsters'][target]['slot']==dest:return 'Цель уже в этой ячейке'
 r['reaction']=dict(player=1-i,initiator=initiator,target=target,dest=dest,type=typ,deadline=time.time()+10)
 log(r,f'Окно реакции: перемещение {MONSTERS[target]["name"]} (10 секунд)')
 return None

def state(r,i):
 p=r['players'][i];op=r['players'][1-i]
 return dict(rosters=[p['monsters'] for p in r['players']],reaction={k:v for k,v in r['reaction'].items() if k!='discard'} if r.get('reaction') else None,reaction_cards=available_reactions(r,i,r['reaction']) if r.get('reaction') and r['reaction']['player']==i else [],test_awakened_unlocked=True,turn_serial=r.get('turn_serial',0),code=r['code'],you=i,team=p['team'],joined=r['players'][1]['joined'],turn=r['turn'],round=r['round'],ap=r['ap'],winner=r['winner'],scores=[x['score'] for x in r['players']],tiles=r['tiles'],monsters=r['monsters'],hand=p['hand'],pending=p['pending'],deck_count=len(p['deck']),discard=p['discard'],enemy_hand_count=len(op['hand']),enemy_deck_count=len(op['deck']),log=r['log'],first_turn=r['first_turn'][i],mulligan=p['mulligan'])

@app.get('/')
def home():return render_template('index.html')
@app.post('/api/create')
def create():
 with lock:
  try:r=new_room((request.json or {}).get('deck'),(request.json or {}).get('monsters'))
  except ValueError as e:return error(str(e))
  return jsonify(code=r['code'],token=r['players'][0]['token'],seat=0)
@app.post('/api/join')
def join():
 code=str((request.json or {}).get('code','')).strip().upper()
 with lock:
  r=rooms.get(code)
  if not r:return error('Комната не найдена')
  if r['players'][1]['joined']:return error('Второе место занято')
  try:
   newdeck,roster=validate_deck('air',(request.json or {}).get('deck'),(request.json or {}).get('monsters')) if (request.json or {}).get('deck') is not None else (deck_for('air'),['aerion','sylphida','veyr'])
  except ValueError as e:return error(str(e))
  ab=[x for x in newdeck if CARDS[x]['kind']=='ability']; tr=[x for x in newdeck if CARDS[x]['kind']=='terrain'];random.shuffle(ab);random.shuffle(tr)
  r['players'][1]['monsters']=roster
  for mid,ms in r['monsters'].items():
   if MONSTERS[mid]['team']=='air':ms['inactive']=mid not in roster;ms['dead']=mid not in roster
  r['players'][1]['hand']=ab[:3]+tr[:1]
  r['players'][1]['deck']=ab[3:]+tr[1:];random.shuffle(r['players'][1]['deck'])
  r['players'][1]['joined']=True;log(r,'Игрок Воздуха присоединился');return jsonify(code=code,token=r['players'][1]['token'],seat=1)
@app.get('/api/state')
def get_state():
 code=request.args.get('code','').upper();t=request.args.get('token','')
 with lock:
  r=rooms.get(code)
  if not r:return error('Комната не найдена')
  i=actor(r,t)
  if i is None:return error('Нет доступа к комнате')
  if r.get('reaction') and time.time()>=r['reaction']['deadline']:
   resolve_reaction(r)
  return jsonify(state(r,i))
@app.get('/api/catalog')
def get_catalog():return jsonify(monsters=MONSTERS,cards=CARDS)
@app.post('/api/action')
def action():
 data=request.json or {};code=str(data.get('code','')).upper()
 with lock:
  r=rooms.get(code)
  if not r:return error('Комната не найдена')
  i=actor(r,data.get('token',''))
  if i is None:return error('Нет доступа')
  if r['winner'] is not None:return error('Матч завершён')
  p=r['players'][i];kind=data.get('type');active=(i==r['turn']);mon=data.get('monster');slot=data.get('slot');card=data.get('card')
  if kind not in ('mulligan','end','draw','pick','place','summon','recall','attack','card','adjust','force','remove_tile','react'):return error('Неизвестное действие')
  if kind not in ('mulligan','card','react') and not active:return error('Сейчас ход соперника')
  if kind in ('place','summon','recall','attack','card','force','remove_tile') and r['ap']<=0 and not(kind=='card' and CARDS.get(card,{}).get('ap')==0):return error('Нет очков действия')
  if r.get('reaction') and time.time()>=r['reaction']['deadline']:resolve_reaction(r)
  if r.get('reaction') and kind!='react':
   return error('Ожидается реакция соперника. Сначала разрешите её.')
  if kind=='react':
   q=r.get('reaction')
   if not q:return error('Окно реакции закрыто')
   if q['player']!=i:return error('Реагирует только защищающийся игрок')
   response=data.get('card')
   options=available_reactions(r,i,q)
   if response:
    if response not in options:return error('Эта реакция сейчас недоступна')
    p['hand'].remove(response);p['discard'].append(response)
    rc=CARDS[response]
    if rc['awakened'] and rc['owner']:r['monsters'][rc['owner']]['awakened']=True
    log(r,f'{i+1}: реакция «{rc["name"]}»')
   elif data.get('cancel'):
    return error('Для отмены принудительного перемещения выберите карту реакции')
   resolve_reaction(r,cancel=response in ('sylphida_a2','ignis_a4','eylar_a2'),card=response)
   return jsonify(state(r,i))
  if kind=='mulligan':
   if p['mulligan'] or not r['first_turn'][i]:return error('Муллиган недоступен')
   selected=data.get('cards',[])
   if not isinstance(selected,list) or len(selected)!=len(set(selected)):return error('Некорректный выбор')
   # repeated ability cards may be selected with index instead of ID
   idx=data.get('indices',[])
   if not isinstance(idx,list) or len(idx)!=len(set(idx)) or any(not isinstance(x,int) or x<0 or x>=len(p['hand']) for x in idx):return error('Некорректные индексы')
   removed=[p['hand'][x] for x in idx];p['hand']=[x for j,x in enumerate(p['hand']) if j not in idx]
   p['deck']+=removed;random.shuffle(p['deck'])
   for old in removed:
    matches=[j for j,x in enumerate(p['deck']) if CARDS[x]['kind']==CARDS[old]['kind']]
    if not matches:return error('В колоде нет карты нужного типа')
    k=random.choice(matches);p['hand'].append(p['deck'].pop(k))
   p['mulligan']=True;log(r,f'Игрок {i+1} сделал муллиган ({len(idx)} карт)')
  elif kind=='draw':
   if r['first_turn'][i]:return error('В первом собственном ходе добора нет')
   if p['pending']:return error('Сначала выберите карту из двух')
   if p.get('drawn_round')==r['round']:return error('Добор за этот ход уже выполнен')
   if not p['deck']:
    p['deck']=p['discard'];p['discard']=[];random.shuffle(p['deck'])
   p['pending']=[p['deck'].pop() for _ in range(min(2,len(p['deck'])))]
   p['drawn_round']=r['round'];log(r,f'Игрок {i+1} открыл карты для добора')
  elif kind=='pick':
   if not p['pending']:return error('Нет карт для выбора')
   k=int(data.get('index',0))
   if k<0 or k>=len(p['pending']):return error('Некорректный индекс')
   selected=p['pending'].pop(k);p['hand'].append(selected)
   for rejected in p['pending']:p['deck'].insert(random.randrange(len(p['deck'])+1),rejected)
   p['pending']=[];log(r,f'Игрок {i+1} выбрал карту')
  elif kind=='place':
   if not active:return error('Не ваш ход')
   if card not in p['hand'] or CARDS[card]['kind']!='terrain':return error('Нет такой территории в руке')
   if not isinstance(slot,int) or not 0<=slot<6 or r['tiles'][slot]:return error('Ячейка занята')
   # Slot with monsters cannot receive a normal new territory
   if any(s['slot']==slot for s in r['monsters'].values()):return error('Нельзя разыграть территорию под монстрами')
   r['tiles'][slot]=dict(card=card,owner=i,clash=False)
   p['hand'].remove(card);r['ap']-=1;log(r,f'{i+1}: территория «{CARDS[card]["name"]}» размещена в слоте {slot+1}')
  elif kind=='summon':
   if mon not in r['players'][i]['monsters']:return error('Монстр не входит в выбранную команду')
   s=r['monsters'][mon]
   if s['dead'] or s['slot'] is not None:return error('Монстр не в резерве')
   if not isinstance(slot,int) or not 0<=slot<6 or r['tiles'][slot] is None:return error('Нужна существующая территория')
   enemies=[m for m,ms in r['monsters'].items() if ms['slot']==slot and owner(m)!=i]
   enter(r,mon,slot);r['ap']-=1;log(r,f'{i+1}: {MONSTERS[mon]["name"]} призван в слот {slot+1}')
   cleanup(r)
  elif kind=='recall':
   if mon not in r['players'][i]['monsters']:return error('Монстр не входит в выбранную команду')
   s=r['monsters'][mon];sl=s['slot']
   if sl is None:return error('Монстр не на поле')
   if any(ms['slot']==sl and owner(m)!=i for m,ms in r['monsters'].items()):return error('Нельзя отозвать монстра из столкновения без способности')
   leave(r,mon);r['ap']-=1;log(r,f'{MONSTERS[mon]["name"]} отозван в резерв')
  elif kind=='attack':
   target=data.get('target')
   if mon not in r['players'][i]['monsters'] or target not in r['players'][1-i]['monsters']:return error('Некорректная пара')
   a=r['monsters'][mon];b=r['monsters'][target]
   if a['dead'] or b['dead'] or a['slot'] is None or b['slot']!=a['slot']:return error('Обычная атака доступна только в одном столкновении')
   bonus=slot_bonus(r,mon)
   damage=max(0,a['attack']+a['bonus']+bonus-a.get('weaken',0))
   consume_buff(r,mon);r['ap']-=1
   q=dict(type='damage',player=1-i,target=target,attacker=mon,source='basic',amount=damage,deadline=time.time()+10)
   if available_reactions(r,1-i,q):
    r['reaction']=q;log(r,f'{MONSTERS[mon]["name"]} атакует {MONSTERS[target]["name"]}. Окно реакции 10 секунд')
   else:
    dealt=hit(r,target,damage)
    if a.pop('first_attack_burn',False) and not b['dead']:apply_burn(r,target,1)
    log(r,f'{MONSTERS[mon]["name"]} атакует {MONSTERS[target]["name"]}: {dealt} CP')
    cleanup(r)
  elif kind=='card':
   if card not in p['hand'] or CARDS[card]['kind']!='ability':return error('Нет этой карты в руке')
   if CARDS[card]['owner'] and CARDS[card]['owner'] not in p['monsters']:return error('Карта не принадлежит выбранной команде')
   c=CARDS[card];m=c['owner'];target=data.get('target');dest=data.get('slot')
   if c['team']=='neutral':
    if c['ap']==0:return error('«Последний заслон» применяется в окно реакции на атаку; пока используйте режим ведущего для этого типа реакции')
    if not active or r['ap']<c['ap']:return error('Нельзя разыграть карту сейчас')
    if target not in p['monsters'] or r['monsters'][target]['dead']:return error('Выберите живого союзника')
    t=r['monsters'][target]; idx=int(card.split('_a')[-1])
    if idx==0:
     enemy=data.get('enemy')
     if enemy not in MONSTERS or owner(enemy)==i or r['monsters'][enemy]['slot']!=t['slot'] or t['slot'] is None:return error('Выберите цель атаки в том же столкновении')
     hit(r,enemy,t['attack']+t['bonus']+slot_bonus(r,target)+20);consume_buff(r,target);hit(r,target,15)
    elif idx==2:
     if t['slot'] is None:return error('Монстр должен быть на поле')
     t['cp']=min(t['max'],t['cp']+20);t['bonus']=-10;t['buff_expire']=r['round']+1
    elif idx==3:
     if t['slot'] is None:return error('Монстр должен быть на поле')
     choice=data.get('choice')
     if choice not in ('heal','cleanse'):return error('Выберите эффект: лечение или очищение')
     if choice=='heal':t['cp']=min(t['max'],t['cp']+10)
     else:t['burn']=max(0,t['burn']-2)
    elif idx==4:
     if t['slot'] is None:return error('Выберите монстра на поле')
     occupied=any(owner(e)!=i and v['slot']==t['slot'] and not v['dead'] for e,v in r['monsters'].items())
     if not occupied:return error('Монстр не находится в столкновении')
     leave(r,target)
    elif idx==5:
     replacement=data.get('replacement')
     if t['slot'] is None or replacement not in MONSTERS or owner(replacement)!=i or replacement==target or r['monsters'][replacement]['dead'] or r['monsters'][replacement]['slot'] is not None:return error('Выберите бойца на поле и союзника из резерва')
     where=t['slot'];leave(r,target);enter(r,replacement,where)
    elif idx in (6,7):
     return error('Для этой карты нужен интерфейс выбора нескольких карт. Пока разыграйте её через ведущего, не списывая карту автоматически.')
    else:return error('Неизвестная нейтральная карта')
    p['hand'].remove(card);p['discard'].append(card);r['ap']-=c['ap'];log(r,f'{i+1}: нейтральная карта «{c["name"]}»');cleanup(r)
    return jsonify(state(r,i))
   s=r['monsters'][m]
   if owner(m)!=i or s['dead']:return error('Монстр недоступен')
   if s['slot'] is None and 'из резерва' not in c['effect'].lower():return error('Способность требует монстра на поле')
   if c['ap']==0:return error('Реакции разыгрываются в открытом окне реакции')
   if not active or r['ap']<c['ap']:return error('Нельзя разыграть карту сейчас')
   if target is not None and (target not in MONSTERS or r['monsters'][target]['dead']):return error('Некорректная цель')
   def ally():return target in MONSTERS and owner(target)==i
   def enemy():return target in MONSTERS and owner(target)!=i
   def sametile():return target in MONSTERS and s['slot']==r['monsters'][target]['slot']
   idx=int(card.split('_a')[-1]);handled=True
   if m in ('kaldra','lavrix','boreal','eylar'):
    problem=awaken_skill(r,i,m,idx,target,dest)
    if problem:return error(problem)
   elif m=='ignis':
    if idx in (0,1,7):s['bonus']={0:20,1:10,7:15}[idx];s['buff_hits']=2 if idx in (1,7) else 1
    elif idx in (2,3,5,6,9):
     if idx!=9 and (not enemy() or not sametile()):return error('Выберите врага на той же территории')
     if idx==2:
      hit(r,target,s['attack']+s['bonus']+slot_bonus(r,m));consume_buff(r,m);apply_burn(r,target,1)
     elif idx==3:hit(r,target,35+(15 if r['monsters'][target]['burn'] else 0))
     elif idx==5:
      amount=r['monsters'][target]['burn'];r['monsters'][target]['burn']=0;hit(r,target,10*amount)
     elif idx==6:
      if s['cp']>30:return error('Нужно не более 30 CP')
      for _ in range(2):hit(r,target,s['attack']+s['bonus']+slot_bonus(r,m));consume_buff(r,m)
     elif idx==9:
      for e,t in r['monsters'].items():
       if owner(e)!=i and t['slot']==s['slot'] and not t['dead']:hit(r,e,20);apply_burn(r,e,1)
    elif idx==8:
     if not enemy():return error('Выберите вражеского монстра')
     reason=queue_force(r,i,m,target,s['slot'])
     if reason:return error(reason)
    else:handled=False
   elif m=='solaris':
    if idx in (0,1,2,3,5,6,7,8,9):
     if idx==6:
      for e,v in r['monsters'].items():
       if owner(e)==i and not v['dead']:v['cp']=min(v['max'],v['cp']+15)
     elif idx==5:return error('«Последняя искра» разыгрывается в окне реакции на смертельный урон')
     elif not ally():return error('Выберите союзника')
     a=r['monsters'][target] if ally() else s
     if idx==0:a['bonus']=10;a['buff_expire']=r['round']+3
     elif idx==1:a['cp']=min(a['max'],a['cp']+25)
     elif idx==2:a['shield']=15;a['shield_hits']=2
     elif idx==3:a['notes']='Первый удар накладывает горение (тестовый эффект)'
     elif idx==6:pass
     elif idx==7:a['burn']=0;a['cp']=min(a['max'],a['cp']+10)
     elif idx==8:
      if target==m or a['slot'] is None or a['slot']!=s['slot']:return error('Выберите другого союзника на территории Солариса')
      for whom in (m,target):
       q=r['monsters'][whom];q['bonus']=10;q['buff_expire']=0;q['linked_until_leave']=True;q['linked_partner']=target if whom==m else m
     elif idx==9:
      hit(r,m,20);a['cp']=min(a['max'],a['cp']+35);a['bonus']=10;a['buff_hits']=1
     else:handled=False
    elif idx==4:
     if not ally() or r['monsters'][target]['slot'] is not None:return error('Выберите союзника из резерва')
     enter(r,target,s['slot'])
    else:handled=False
   elif m=='magmor':
    if idx==0:
     if not isinstance(dest,int) or not 0<=dest<6 or not r['tiles'][dest]:return error('Укажите существующую территорию')
     r['tiles'][dest]['hot_until']=r['round']+1;r['tiles'][dest]['hot_owner']=i
    elif idx==1:s['shield']=15;s['shield_hits']=2
    elif idx==2:
     if not enemy():return error('Выберите вражеского монстра')
     reason=queue_force(r,i,m,target,s['slot'])
     if reason:return error(reason)
    elif idx==4:
     if not isinstance(dest,int) or not 0<=dest<6 or not r['tiles'][dest]:return error('Укажите территорию')
     old=r['tiles'][dest]
     r['players'][old['owner']]['discard'].append(old['card'])
     r['tiles'][dest]=None
     for e,t in r['monsters'].items():
      if t['slot']==dest and owner(e)!=i:apply_burn(r,e,1)
    elif idx==3:
     if not isinstance(dest,int) or not 0<=dest<6:return error('Укажите ячейку')
     for e,t in r['monsters'].items():
      if owner(e)!=i and t['slot']==dest and not t['dead']:hit(r,e,15);apply_burn(r,e,1)
    elif idx==5:
     if not enemy():return error('Выберите вражеского монстра')
     dmg=min(40,10*sum(bool(tile and CARDS[tile['card']]['team']=='fire') for tile in r['tiles']))
     hit(r,target,dmg)
    elif idx==6:
     for mon2,ms in r['monsters'].items():
      if owner(mon2)==i and ms['slot']==s['slot'] and not ms['dead']:
       ms['bonus']=15;ms['buff_expire']=r['round']+1
    elif idx==7:
     if s['slot'] is None or not r['tiles'][s['slot']]:return error('Магмор должен стоять на территории')
     r['tiles'][s['slot']]['furnace_until']=r['round']+1
     r['tiles'][s['slot']]['furnace_owner']=i
    elif idx==8:
     if not isinstance(dest,int) or not 0<=dest<6 or r['tiles'][dest] is None:return error('Выберите существующую территорию')
     r['tiles'][dest]['lava_next']=2;r['tiles'][dest]['lava_owner']=i
    elif idx==9:
     for enemy_id,enemy_state in r['monsters'].items():
      if owner(enemy_id)!=i and not enemy_state['dead']:
       enemy_state['burn']=max(0,enemy_state['burn']-1);hit(r,enemy_id,15)
    else:handled=False
   elif m=='sylphida':
    if idx in (0,1,3,4,6,7,8,9):
     if idx in (6,9):pass
     elif not ally():return error('Выберите союзника')
     a=r['monsters'][target] if ally() else s
     if idx==0:a['shield']=15;a['shield_hits']=2
     elif idx==1:a['burn']=0
     elif idx==3:a['cp']=min(a['max'],a['cp']+20)
     elif idx==4:a['bonus']=10;a['buff_hits']=1
     elif idx==6:
      allies=[v for mm,v in r['monsters'].items() if owner(mm)==i and not v['dead'] and v['burn']>0]
      for av in allies[:2]:av['burn']=0
     elif idx==7:a['shield']=20;a['shield_hits']=2
     elif idx==8:
      if not isinstance(dest,int) or not 0<=dest<6 or not r['tiles'][dest]:return error('Выберите существующую территорию')
      if a['slot'] is None or a['slot']==dest:return error('Союзник должен находиться на другой территории')
      leave(r,target);enter(r,target,dest)
     elif idx==9:
      for e,v in r['monsters'].items():
       if owner(e)==i and not v['dead']:v['cp']=min(v['max'],v['cp']+15)
     else:handled=False
    else:handled=False
   elif m=='aerion':
    if idx in (0,3):s['bonus']=20 if idx==0 else 10;s['buff_hits']=1 if idx==0 else 2
    elif idx in (1,2,5):
     if not enemy() or not sametile():return error('Выберите врага на своей территории')
     if idx==2:hit(r,target,s['attack']+s['bonus']+slot_bonus(r,m));consume_buff(r,m);leave(r,m)
     else:hit(r,target,35 if idx==1 else 45)
    elif idx==4:
     if not enemy() or not sametile():return error('Выберите врага на своей территории')
     if not isinstance(dest,int) or not 0<=dest<6 or not r['tiles'][dest] or dest==s['slot']:return error('Выберите другую существующую территорию')
     hit(r,target,20);leave(r,m);enter(r,m,dest)
    elif idx==6:
     if s['slot'] is None:return error('Аэрион должен находиться на поле')
     leave(r,m);s['shield']=15;s['shield_hits']=1
    elif idx==7:
     for enemy_id,enemy_s in r['monsters'].items():
      if owner(enemy_id)!=i and enemy_s['slot']==s['slot'] and not enemy_s['dead']:hit(r,enemy_id,25)
    elif idx==8:return error('«Перо бури» разыгрывается в окне реакции на урон')
    elif idx==9:
     if not isinstance(dest,int) or not 0<=dest<6 or not r['tiles'][dest]:return error('Выберите существующую территорию')
     for enemy_id,enemy_s in r['monsters'].items():
      if owner(enemy_id)!=i and enemy_s['slot']==dest and not enemy_s['dead']:hit(r,enemy_id,20)
    else:handled=False
   elif m=='veyr':
    if idx in (0,1,7):
     if not enemy():return error('Выберите вражеского монстра')
     if idx==1 and not sametile():return error('Цель должна быть на территории Вейра')
     if idx==0 and r['monsters'][target]['slot'] is not None:return error('Выберите врага в резерве')
     reason=queue_force(r,i,m,target,s['slot'],typ='push' if idx==1 else 'pull')
     if reason:return error(reason)
    elif idx==3:s['shield']=10;s['shield_hits']=2
    elif idx==4:s['bonus']=15;s['buff_hits']=1
    elif idx in (2,5):
     if not enemy():return error('Выберите вражеского монстра')
     if not isinstance(dest,int) or not 0<=dest<6 or not r['tiles'][dest]:return error('Выберите существующую территорию')
     if r['monsters'][target]['slot']==dest:return error('Цель уже находится на этой территории')
     if idx==2:hit(r,target,20)
     if not r['monsters'][target]['dead']:
      reason=queue_force(r,i,m,target,dest)
      if reason:return error(reason)
    elif idx==6:
     if not enemy():return error('Выберите врага')
     r['monsters'][target]['no_terrain_buff_until']=r['round']+1
    elif idx==8:return error('«Срыв атаки» разыгрывается в окне реакции на атаку')
    elif idx==9:
     if not isinstance(dest,int) or not 0<=dest<6 or not r['tiles'][dest]:return error('Выберите существующую территорию')
     for enemy_id,enemy_s in r['monsters'].items():
      if owner(enemy_id)!=i and enemy_s['slot']==dest and not enemy_s['dead']:hit(r,enemy_id,25)
    else:handled=False
   if not handled:return error('Для этой карты автоматический эффект ещё не задан. Используйте ручной режим ведущего.')
   if card in p['hand']:
    p['hand'].remove(card)
    if not s['dead']:p['discard'].append(card)
   r['ap']-=c['ap']
   if c['awakened'] and not s['dead']:s['awakened']=True
   log(r,f'{i+1}: «{c["name"]}», цель {MONSTERS[target]["name"] if target in MONSTERS else "территория"}')
   cleanup(r)
  elif kind=='adjust':
   # Manual referee adjustments: only active player can manage results of effects; logged, not automatic.
   if mon not in r['players'][i]['monsters'] or r['monsters'][mon]['dead']:return error('Монстр недоступен')
   field=data.get('field');s=r['monsters'][mon]
   if field not in ('cp','burn','shield','bonus'):return error('Неверное поле')
   delta=int(data.get('delta',0))
   if delta<-1000 or delta>1000:return error('Изменение слишком велико')
   if field=='cp':
    if delta<0:hit(r,mon,-delta)
    else:s['cp']=min(s['max'],s['cp']+delta)
   elif field=='burn':s['burn']=max(0,min(5,s['burn']+delta))
   else:s[field]=max(0,s[field]+delta) if field=='shield' else s[field]+delta
   log(r,f'Ручная корректировка {MONSTERS[mon]["name"]}: {field} {delta:+}')
   cleanup(r)
  elif kind=='force':
   return error('Принудительное перемещение выполняется картами способностей, а не свободным действием')
  elif kind=='remove_tile':
   if not isinstance(slot,int) or not 0<=slot<6 or not r['tiles'][slot]:return error('Нет территории')
   t=r['tiles'][slot];r['players'][t['owner']]['discard'].append(t['card']);
   for m,st in r['monsters'].items():
    if st['slot']==slot and st.get('heart'):
     st['heart']=False;st['max']-=20;st['cp']-=20
     if st['cp']<=0:hit(r,m,0)
   r['tiles'][slot]=None;r['ap']-=1
   log(r,f'Территория в слоте {slot+1} уничтожена; монстры остаются на месте')
  elif kind=='end':
   if p['pending']:return error('Сначала завершите добор')
   discards=data.get('discard',[])
   expected=max(0,len(p['hand'])-4)
   if expected:
    from collections import Counter
    if not isinstance(discards,list) or len(discards)!=expected or not Counter(discards)<=Counter(p['hand']):return error(f'Выберите {expected} карт для сброса')
   else:discards=[]
   burning=[m for m,st in r['monsters'].items() if owner(m)==i and not st['dead'] and st['burn']>0]
   if burning:
    q=dict(type='burn',player=i,target=burning[0],source='negative',discard=discards,deadline=time.time()+10)
    if available_reactions(r,i,q):
     r['reaction']=q;log(r,'Окно реакции: урон от горения (10 секунд)')
    else:finish_turn(r,i,discards)
   else:finish_turn(r,i,discards)
  return jsonify(state(r,i))

if __name__=='__main__':
 app.run(host=os.getenv('HOST','0.0.0.0'),port=int(os.getenv('PORT','5000')),debug=False)

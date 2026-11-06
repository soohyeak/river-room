# -*- coding: utf-8 -*-
"""리버룸 — Pydroid용 단일 파일 웹 홀덤.
이 파일 하나를 Pydroid에서 열고 실행하세요. 추가 설치/압축 해제 불필요.
실행 후 브라우저 주소창에서 http://127.0.0.1:8000 을 여세요.
같은 Wi-Fi 초대 링크는 Wi-Fi IPv4 주소로 접속한 뒤 만드세요.
서로 다른 장소에서 플레이하려면 외부 HTTPS 호스팅에 배포해야 합니다.
가상 칩 전용. 저장/실제 결제 없음. 서버 종료 시 방과 칩은 사라집니다.
"""
import hmac
import io
import json
import os
import re
import secrets
import threading
import time
import unicodedata
from collections import OrderedDict, deque
from urllib.parse import parse_qs, urlsplit

# ---- Bundled poker rules (no engine.py import required) ----
"""Server-only two-player poker rules. No graphics or networking."""
import random
from collections import Counter
from itertools import combinations

START_CHIPS = 1000
SMALL_BLIND = 10
BIG_BLIND = 20
KOREAN = True
DECK = [(r, s) for s in range(4) for r in range(2, 15)]
KO_HANDS = ["하이카드", "원페어", "투페어", "트리플", "스트레이트",
            "플러시", "풀하우스", "포카드", "스트레이트 플러시"]
EN_HANDS = ["High card", "One pair", "Two pair", "Three of a kind", "Straight",
            "Flush", "Full house", "Four of a kind", "Straight flush"]


def tr(ko, en):
    return ko if KOREAN else en


def hand_name(score):
    if score[:2] == (8, 14):
        return tr("로열 플러시", "Royal flush")
    return (KO_HANDS if KOREAN else EN_HANDS)[score[0]]


def score5(cards):
    ranks = sorted((r for r, s in cards), reverse=True)
    groups = sorted(((n, r) for r, n in Counter(ranks).items()), reverse=True)
    unique = sorted(set(ranks), reverse=True)
    straight = 0
    if len(unique) == 5:
        if unique[0] - unique[-1] == 4:
            straight = unique[0]
        elif unique == [14, 5, 4, 3, 2]:
            straight = 5
    flush = len({s for r, s in cards}) == 1
    ordered = tuple(r for n, r in groups)
    if flush and straight:
        return (8, straight)
    if groups[0][0] == 4:
        return (7, *ordered)
    if groups[0][0] == 3 and groups[1][0] == 2:
        return (6, *ordered)
    if flush:
        return (5, *ranks)
    if straight:
        return (4, straight)
    if groups[0][0] == 3:
        return (3, *ordered)
    if groups[0][0] == 2 and groups[1][0] == 2:
        return (2, *ordered)
    if groups[0][0] == 2:
        return (1, *ordered)
    return (0, *ranks)


def best_score(cards):
    return max(score5(c) for c in combinations(cards, 5))


class Game:
    """2인 노리밋 홀덤. 화면 코드와 분리된 게임 규칙입니다."""
    def __init__(self, names):
        self.names = list(names)
        self.chips = [START_CHIPS, START_CHIPS]
        self.button = 0
        self.hand_no = 0
        self.revision = 0
        self.phase = "over"
        self.turn = None
        self.pot = 0
        self.start_hand(0)

    def who(self, i):
        return self.names[i]

    @property
    def stage_name(self):
        return (["프리플롭", "플롭", "턴", "리버"] if KOREAN else
                ["PREFLOP", "FLOP", "TURN", "RIVER"])[self.stage]

    def check(self):
        assert sum(self.chips) + self.pot == START_CHIPS * 2
        assert all(v >= 0 for v in self.chips)
        assert self.pot >= 0

    def pay(self, who, amount):
        amount = min(max(0, amount), self.chips[who])
        self.chips[who] -= amount
        self.bets[who] += amount
        self.pot += amount
        self.check()

    def refund(self):
        delta = self.bets[0] - self.bets[1]
        if delta:
            i = 0 if delta > 0 else 1
            amount = abs(delta)
            self.bets[i] -= amount
            self.chips[i] += amount
            self.pot -= amount
            self.note = tr(f"미매칭 베팅 {amount}칩은 반환되었습니다.",
                           f"Uncalled {amount} chips returned.")
        self.check()

    def start_hand(self, now):
        if min(self.chips) <= 0:
            return False
        if self.hand_no:
            self.button = 1 - self.button
        self.hand_no += 1
        self.revision += 1
        self.pot = 0
        self.last_pot = 0
        self.stage = 0
        self.bets = [0, 0]
        self.board = []
        self.holes = [[], []]
        self.deck = DECK.copy()
        random.SystemRandom().shuffle(self.deck)
        for _ in range(2):
            for i in (1 - self.button, self.button):
                self.holes[i].append(self.deck.pop())
        self.revealed = False
        self.showdown = False
        self.best_cards = [set(), set()]
        self.winners = []
        self.title = ""
        self.detail = ""
        self.note = tr("레이즈 금액은 이번 거리의 내 총베팅액입니다.",
                       "Raise amount is your TOTAL bet on this street.")
        self.log = tr("블라인드가 지급되었습니다.", "Blinds posted.")
        self.pay(self.button, SMALL_BLIND)
        self.pay(1 - self.button, BIG_BLIND)
        self.begin_round(self.button, now)
        return True

    def begin_round(self, first, now):
        self.last_raise = BIG_BLIND
        self.pending = {i for i in range(2) if self.chips[i] > 0}
        self.phase = "betting"
        self.choose_turn(first, now)

    def choose_turn(self, first, now):
        for i in (first, 1 - first):
            if i not in self.pending:
                continue
            if self.chips[i] == 0 or (
                self.chips[1 - i] == 0 and self.bets[i] >= self.bets[1 - i]
            ):
                self.pending.discard(i)
                continue
            self.turn = i
            return
        self.turn = None
        self.refund()
        self.phase = "pause"
        self.deadline = now + 0.9
        if min(self.chips) == 0:
            self.revealed = True

    def legal(self, who):
        high = max(self.bets)
        call = high - self.bets[who]
        maximum = self.bets[who] + self.chips[who]
        minimum = high + self.last_raise
        can_raise = maximum > high and self.chips[1 - who] > 0
        return dict(call=call, maximum=maximum, minimum=minimum,
                    can_raise=can_raise,
                    can_allin=can_raise or (0 < self.chips[who] <= call))

    def act(self, who, action, target=0, now=0):
        if self.phase != "betting" or self.turn != who:
            return False
        opt = self.legal(who)
        high = max(self.bets)
        other = 1 - who
        if action == "fold":
            if opt["call"] == 0:
                return False
            self.log = tr(f"{self.who(who)}: 폴드", f"{self.who(who)} folds")
            self.refund()
            self.finish([other], False)
        elif action == "raise":
            if not opt["can_raise"]:
                return False
            if not (high < target <= opt["maximum"]):
                return False
            if target < opt["minimum"] and target != opt["maximum"]:
                return False
            self.pay(who, target - self.bets[who])
            increase = self.bets[who] - high
            if increase >= self.last_raise:
                self.last_raise = increase
            self.pending = {other} if self.chips[other] > 0 else set()
            word = tr("올인", "ALL IN") if not self.chips[who] else tr("레이즈", "RAISE")
            self.log = f"{self.who(who)}: {word} {self.bets[who]}"
            self.choose_turn(other, now)
        elif action == "call":
            paid = min(opt["call"], self.chips[who])
            self.pay(who, opt["call"])
            word = (tr(f"콜 {paid}", f"CALL {paid}") if opt["call"] else
                    tr("체크", "CHECK"))
            if not self.chips[who]:
                word += tr(" · 올인", " · ALL IN")
            self.log = f"{self.who(who)}: {word}"
            self.pending.discard(who)
            self.choose_turn(other, now)
        else:
            return False
        self.revision += 1
        self.check()
        return True

    def advance(self, now):
        if self.phase != "pause" or now < self.deadline:
            return False
        if self.stage == 3:
            self.revealed = True
            self.showdown = True
            scores = []
            for i in range(2):
                best = max(combinations(self.holes[i] + self.board, 5), key=score5)
                self.best_cards[i] = set(best)
                scores.append(score5(best))
            winners = ([0, 1] if scores[0] == scores[1] else
                       [0] if scores[0] > scores[1] else [1])
            self.detail = f"{self.who(0)}: {hand_name(scores[0])}   |   {self.who(1)}: {hand_name(scores[1])}"
            self.finish(winners, True)
        else:
            self.stage += 1
            self.bets = [0, 0]
            self.deck.pop()
            count = 3 if self.stage == 1 else 1
            self.board.extend(self.deck.pop() for _ in range(count))
            self.log = tr(f"{self.stage_name} 공개", f"{self.stage_name} dealt")
            self.begin_round(1 - self.button, now)
        self.revision += 1
        return True

    def finish(self, winners, at_showdown):
        self.winners = winners
        self.last_pot = self.pot
        if len(winners) == 2:
            share, odd = divmod(self.pot, 2)
            for i in range(2):
                self.chips[i] += share
            self.chips[1 - self.button] += odd
            self.title = tr(f"무승부 · 팟 {self.pot} 분배", f"SPLIT POT · {self.pot}")
        else:
            winner = winners[0]
            self.chips[winner] += self.pot
            self.title = tr(f"{self.who(winner)} 승리! +{self.pot}칩",
                            f"{self.who(winner)} WINS +{self.pot}")
        if not at_showdown:
            self.detail = self.log
        self.pot = 0
        self.turn = None
        self.phase = "over"
        self.check()





# ---- Bundled HTML (no index.html file required) ----
HTML_PAGE = '<!doctype html>\n<html lang="ko">\n<head>\n<meta charset="utf-8">\n<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">\n<meta name="theme-color" content="#0b1b18">\n<meta name="description" content="친구에게 링크를 보내 함께하는 2인 텍사스 홀덤. 실제 돈을 사용하지 않습니다.">\n<title>리버룸 · 친구와 하는 홀덤</title>\n<style>\n:root{color-scheme:dark;--bg:#0b1b18;--panel:#142823;--line:#355147;--felt:#1d5b45;--gold:#ecc384;--muted:#adc0b5;--text:#f5f2e8;--green:#32765d;--red:#c45d60;--dim:#81978b}\n*{box-sizing:border-box}body{margin:0;background:radial-gradient(ellipse at 50% 25%,#15372c,transparent 65%),var(--bg);color:var(--text);font-family:system-ui,\'Apple SD Gothic Neo\',\'Nanum Gothic\',sans-serif;font-size:16px;min-height:100svh}button,input{font:inherit}button{cursor:pointer;touch-action:manipulation;border:1px solid var(--line);color:var(--text);background:var(--panel);border-radius:14px;min-height:48px;padding:12px 19px;font-weight:700;transition:background .15s,transform .15s}button:hover:not(:disabled){filter:brightness(1.1)}button:active:not(:disabled){transform:translateY(1px)}button:disabled{cursor:default;opacity:.43}button:focus-visible,input:focus-visible,summary:focus-visible,a:focus-visible{outline:3px solid var(--gold);outline-offset:3px}a{color:var(--gold)}button.primary{background:var(--gold);color:#17251e;border-color:var(--gold)}button.green{background:#2e7357;border-color:#4a9170}button.danger{background:#773a3f;border-color:#aa555c}button.quiet{background:transparent;color:var(--muted)}input[type=text]{background:#0f211b;color:var(--text);border:1px solid #52725f;padding:14px;border-radius:12px;min-height:52px;min-width:0;width:100%}input::placeholder{color:#8da799}h1,h2,p{margin-top:0}h1{font-size:clamp(34px,5vw,58px);line-height:1.18;letter-spacing:-2px;margin-bottom:24px}h2{font-size:24px;letter-spacing:-.7px}p{line-height:1.7;color:var(--muted)}[hidden]{display:none!important}.wrap{max-width:1140px;margin:0 auto;padding:0 24px}.topbar{display:flex;align-items:center;justify-content:space-between;padding:24px 0;gap:16px;border-bottom:1px solid #2c4438}.brand{font-weight:850;letter-spacing:3px;font-size:22px;display:flex;gap:12px;align-items:center}.brand .spade{font-size:32px;color:var(--gold)}.subbrand{font-size:10px;letter-spacing:2px;color:var(--muted);margin-top:3px}.tag{border:1px solid #47684f;border-radius:99px;color:#d1e4d3;padding:7px 13px;font-size:12px;white-space:nowrap}.dot{display:inline-block;width:7px;height:7px;border-radius:100%;background:#91cda3;margin-right:7px}.offline .dot{background:var(--gold)}.hero{padding:70px 0 40px;display:grid;grid-template-columns:1fr .85fr;gap:60px;align-items:center}.eyebrow{font-size:11px;color:var(--gold);letter-spacing:3px;margin-bottom:22px;font-weight:800}.accent{color:var(--gold)}.intro{max-width:450px;margin-bottom:28px}.entry{padding:28px;background:#142b22;border:1px solid #3f604c;border-radius:24px;box-shadow:0 24px 70px #0003}.entry .label{display:block;color:var(--muted);font-size:13px;margin:0 0 9px}.entry button.primary{width:100%;margin-top:14px;font-size:17px}.small{font-size:12px;line-height:1.65;color:var(--muted)}.entry p.small{margin:16px 0 0}.home-art{min-height:280px;background:radial-gradient(ellipse,#286649,#163b2b 65%);border:7px solid #294b36;border-radius:50%;position:relative;display:flex;flex-direction:column;align-items:center;justify-content:center;box-shadow:inset 0 0 0 2px #66835377}.art-cards{display:flex;gap:10px;transform:rotate(-8deg);margin-bottom:18px}.art-caption{color:#c0d8bb;font-size:11px;letter-spacing:3px}.feature-row{display:grid;grid-template-columns:repeat(3,1fr);gap:20px;padding:35px 0 50px;border-bottom:1px solid #2c4438}.feature{border-top:1px solid #5c7355;padding:17px 0 0}.feature .number{font-size:11px;color:var(--gold);margin-bottom:12px;letter-spacing:2px}.feature h2{font-size:17px;margin-bottom:8px}.feature p{font-size:13px;margin-bottom:0}.footer{padding:24px 0 36px;display:flex;align-items:center;justify-content:space-between;gap:16px;font-size:12px;color:var(--muted)}details{font-size:13px;color:var(--muted);line-height:1.8;max-width:800px;padding:12px 0}summary{cursor:pointer;color:var(--gold);font-weight:700}.notice{border:1px solid #9a7448;background:#3d321e;border-radius:12px;padding:12px 16px;color:#ffe0a8;font-size:13px;line-height:1.65;margin-top:18px}.connection{padding:11px 16px;font-size:13px;border-radius:12px;margin-top:16px;background:#422d20;color:#ffe0ae;display:flex;align-items:center;justify-content:space-between;gap:14px}.connection button{min-height:36px;padding:6px 12px}.lobby{max-width:640px;margin:50px auto}.lobby-head{text-align:center;margin-bottom:27px}.lobby-head h1{font-size:36px;letter-spacing:-1.5px}.lobby .entry{padding:30px}.linkbox{display:flex;gap:8px;margin-top:14px}.linkbox input{font-size:12px}.lobby-actions{display:grid;grid-template-columns:1fr;gap:10px}.lobby-members{display:flex;gap:14px;margin:26px 0}.member{flex:1;padding:16px;border:1px solid #3f604c;border-radius:12px;color:var(--muted)}.member b{display:block;color:var(--text);margin:8px 0 4px}.spinner{display:inline-block;width:14px;height:14px;border:2px solid #5e795b;border-top-color:var(--gold);border-radius:50%;animation:spin 1.2s linear infinite;vertical-align:-2px;margin-right:8px}@keyframes spin{to{transform:rotate(360deg)}}.gamehead{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:19px 0}.handtag{font-size:12px;color:var(--muted);letter-spacing:1px}.gamehead button{min-height:38px;padding:7px 12px;font-size:12px}.gamehead-actions{display:flex;gap:8px}.table-area{position:relative;padding:12px 0 0}.seat{display:flex;align-items:center;justify-content:center;gap:22px;position:relative;z-index:2}.opponent{margin-bottom:-25px}.my-seat{margin-top:-22px}.player-info{width:170px;line-height:1.6}.opponent .player-info{text-align:right}.playername{font-weight:750;font-size:15px;overflow-wrap:anywhere}.playerrole{font-size:10px;letter-spacing:1.5px;color:var(--muted)}.balance{color:var(--gold);font-size:24px;font-weight:750;font-variant-numeric:tabular-nums}.chipunit{font-size:10px;letter-spacing:2px;color:var(--muted)}.seat-cards{display:flex;gap:9px;flex-shrink:0}.card{width:clamp(52px,7.8vw,83px);height:clamp(76px,11.5vw,119px);background:#f7f2e5;border:1px solid #d7d5bf;border-radius:9px;color:#233329;box-shadow:0 4px 0 #071c1866;position:relative;display:flex;align-items:center;justify-content:center;font-size:clamp(27px,4vw,44px);font-family:Georgia,serif;user-select:none;flex-shrink:0}.card .rank{position:absolute;left:8px;top:5px;font-size:21px;line-height:1;font-weight:bold}.card .mini{position:absolute;right:7px;bottom:5px;font-size:15px}.card.red{color:#b4404a}.card.back{border:3px solid #e0dcc6;background:repeating-linear-gradient(45deg,#6d3842,#6d3842 4px,#7a4751 4px,#7a4751 6px);color:#eccc9a;font-size:27px}.card.empty{background:#18473577;border:1px dashed #629576;color:#7da887;box-shadow:none;font-size:23px}.card.best{outline:3px solid var(--gold);outline-offset:1px}.felt{border-radius:150px;border:8px solid #334c36;box-shadow:inset 0 0 0 2px #6b835333,0 20px 70px #0003;background:radial-gradient(ellipse at 50% 40%,#236849,#184a37);min-height:285px;display:flex;flex-direction:column;align-items:center;justify-content:center;padding:43px 20px 40px;position:relative}.pot{display:flex;align-items:center;gap:8px;margin-bottom:18px;background:#0f3526a0;border:1px solid #4b7854;border-radius:99px;padding:7px 20px}.pot .caption{color:#b5ceb1;letter-spacing:2px;font-size:11px}.pot strong{font-size:22px;color:#ffda9c;font-variant-numeric:tabular-nums}.board{display:flex;gap:11px;justify-content:center}.betline{display:flex;justify-content:space-between;width:min(540px,100%);font-size:11px;color:#c6ddc0;margin-top:21px}.tablelog{font-size:13px;text-align:center;min-height:24px;margin-top:16px;color:#d4e5cb;max-width:90%;overflow-wrap:anywhere}.dealer{background:#f3deb1;color:#263625;display:inline-flex;width:20px;height:20px;align-items:center;justify-content:center;font-size:11px;font-weight:bold;border-radius:50%;margin-left:6px}.turn-badge{border:1px solid #849c63;color:#ebdbb0;border-radius:99px;padding:3px 9px;font-size:10px;white-space:nowrap;display:inline-block;margin-top:5px}.stack-wrap{width:170px;display:flex;gap:12px;align-items:flex-end;padding:8px 0 10px;min-height:55px}.chip-stack{position:relative;width:32px;height:28px}.chip{display:flex;align-items:center;justify-content:center;border-radius:50%;width:31px;height:31px;position:absolute;left:0;bottom:0;background:#32745b;border:3px dashed #e5e1c5;box-shadow:0 3px 0 #17392f;color:#f6edcc;font-size:8px;font-weight:750}.chip.base{bottom:-5px;filter:brightness(.75)}.chip.gold{background:#9d772d}.chip.blue{background:#315e83}.chip.red{background:#93474c}.actionpanel{margin-top:20px;border:1px solid var(--line);border-radius:21px;background:#142820;box-shadow:0 -6px 30px #0b1b1833;padding:20px;margin-bottom:22px}.action-title{display:flex;justify-content:space-between;align-items:center;gap:12px;margin-bottom:14px;color:var(--gold);font-size:14px}.action-title .hint{font-size:11px;color:var(--muted)}.range-row{display:flex;align-items:center;gap:12px}.range-row input{flex:1;min-width:50px;accent-color:var(--gold);height:38px;cursor:pointer}.range-row button{min-width:46px;min-height:40px;padding:5px 12px;font-size:21px}.range-value{font-size:22px;color:var(--gold);font-weight:750;min-width:70px;text-align:right;font-variant-numeric:tabular-nums}.presets{display:flex;gap:8px;margin:10px 0 16px;justify-content:flex-end}.presets button{font-size:11px;min-height:32px;padding:6px 15px}.bet-actions{display:grid;grid-template-columns:1fr 1.2fr 1.45fr 1fr;gap:10px}.bet-actions button{min-height:60px;font-size:16px;padding:10px 6px}.result{text-align:center;border:1px solid #8b7549;background:#303321;border-radius:16px;padding:20px;margin:24px 0 10px}.result h2{color:var(--gold);font-size:23px;margin-bottom:10px}.result p{margin:7px 0;font-size:13px}.result button{min-width:min(320px,100%);margin-top:12px}.safety{text-align:center;font-size:10px;letter-spacing:.4px;color:#a2b9a7;padding-bottom:20px}.toast{position:fixed;bottom:25px;left:50%;transform:translateX(-50%);padding:15px 22px;background:#ebcf9c;color:#17251e;border-radius:14px;box-shadow:0 10px 40px #0006;z-index:30;font-size:14px;max-width:90vw;width:max-content;line-height:1.6}dialog{color:var(--text);background:#142820;border:1px solid #567052;border-radius:22px;max-width:430px;width:calc(100% - 40px);padding:28px}dialog::backdrop{background:#000b}.dialog-actions{display:flex;gap:12px;margin-top:25px}.dialog-actions button{flex:1}.tiny-button{min-height:36px;font-size:12px;padding:7px 12px}.preview-note{color:#ffda9c;font-size:12px;text-align:center;margin-top:15px}\n@media(max-width:720px){.wrap{padding:0 15px}.topbar{padding:16px 0}.brand{font-size:18px}.brand .spade{font-size:28px}.tag{font-size:10px;padding:6px 9px}.hero{padding-top:36px;grid-template-columns:1fr;gap:26px}.hero h1{letter-spacing:-1.5px}.intro{font-size:14px}.home-art{display:none}.entry{padding:22px}.feature-row{gap:16px;padding:12px 0 32px}.feature h2{font-size:14px}.feature p{font-size:11px}.feature .number{font-size:9px}.footer{flex-direction:column;align-items:flex-start;padding-top:20px;gap:9px}.lobby{margin:30px auto}.lobby .entry{padding:21px}.lobby-head h1{font-size:28px}.linkbox{flex-wrap:wrap}.linkbox input{width:100%;flex-basis:100%}.linkbox button{width:100%}.gamehead{padding:14px 0}.gamehead-actions{gap:6px}.handtag{font-size:10px}.seat{gap:12px}.player-info{width:110px}.playername{font-size:12px}.playerrole{font-size:9px}.balance{font-size:20px}.stack-wrap{width:85px;gap:4px}.chip-stack{width:25px}.chip{width:25px;height:25px;font-size:7px}.card .rank{font-size:17px;left:6px}.felt{padding:42px 10px 35px;min-height:260px;border-width:5px;border-radius:90px}.board{gap:6px}.opponent{margin-bottom:-19px}.my-seat{margin-top:-18px}.seat-cards{gap:7px}.tablelog{font-size:11px}.pot{margin-bottom:16px;padding:6px 15px}.pot strong{font-size:19px}.betline{font-size:10px;margin-top:17px}.actionpanel{padding:14px;margin-top:17px}.action-title{font-size:12px;gap:8px}.action-title .hint{font-size:9px;max-width:135px;text-align:right}.bet-actions{gap:7px}.bet-actions button{font-size:13px;min-height:54px}.range-row{gap:8px}.range-value{min-width:50px;font-size:19px}.presets{margin-bottom:13px}.result{padding:17px 12px}.result h2{font-size:19px}.result p{font-size:12px}.connection{font-size:11px}.member{padding:12px;font-size:12px}.turn-badge{font-size:9px}.safety{font-size:9px}.actionpanel{padding-bottom:max(14px,env(safe-area-inset-bottom))}}\n@media(max-width:360px){.wrap{padding:0 10px}.board{gap:4px}.board .card{width:48px;height:71px}.seat{gap:8px}.player-info{width:90px}.stack-wrap{width:70px}.bet-actions{grid-template-columns:1fr 1fr}.feature-row{grid-template-columns:1fr}.feature p{font-size:13px}}\n@media(prefers-reduced-motion:reduce){*{animation:none!important;transition:none!important}}\n</style>\n</head>\n<body>\n<div class="wrap">\n<header class="topbar"><div><div class="brand"><span class="spade" aria-hidden="true">♠</span> RIVER ROOM</div><div class="subbrand">FRIENDS AT THE SAME TABLE</div></div><span class="tag" id="connectionTag"><span class="dot"></span><span id="tagText">2인 프라이빗 홀덤</span></span></header>\n<div id="offlineNotice" class="notice" hidden>이 파일은 화면 미리보기입니다. 실제 방 만들기·초대 링크는 서버를 배포한 웹 주소에서 사용할 수 있습니다.</div>\n<div id="connectionBanner" class="connection" hidden role="status"><span id="connectionText"></span><button id="retry" type="button">다시 연결</button></div>\n<main>\n<section id="home">\n<div class="hero"><div><div class="eyebrow">JUST YOU, YOUR FRIEND & THE RIVER</div><h1 id="homeTitle">링크 하나로,<br><span class="accent">같은 테이블.</span></h1><p class="intro" id="homeIntro">친구에게 초대 링크를 보내세요.<br>설치 없이, 각자의 화면에서 즐기는 텍사스 홀덤.</p><form class="entry" id="entryForm"><label class="label" for="nickname">테이블에서 사용할 이름</label><input id="nickname" type="text" maxlength="16" autocomplete="nickname" placeholder="닉네임을 입력하세요" required aria-describedby="nameHelp"><button class="primary" id="enter" type="submit">방 만들고 친구 초대하기 →</button><p class="small" id="nameHelp">방 하나에 두 사람만 참가합니다. 시작 칩 1,000 · 블라인드 10/20</p></form><button id="preview" class="quiet tiny-button" type="button" hidden style="margin-top:12px">게임 화면 미리보기</button></div>\n<div class="home-art" aria-hidden="true"><div class="art-cards"><div class="card"><span class="rank">A</span>♠<span class="mini">A</span></div><div class="card red"><span class="rank">K</span>♥<span class="mini">K</span></div></div><div class="art-caption">A LITTLE LUCK. A GOOD FRIEND.</div></div></div>\n<div class="feature-row"><article class="feature"><div class="number">01 / INVITE</div><h2>링크로 간편하게</h2><p>친구에게 공유하면<br>바로 같은 방에 참가합니다.</p></article><article class="feature"><div class="number">02 / PRIVATE</div><h2>내 카드는 나만</h2><p>공용 카드와 팟은 함께,<br>개인 카드는 각자 봅니다.</p></article><article class="feature"><div class="number">03 / JUST FOR FUN</div><h2>부담 없는 가상 칩</h2><p>실제 결제나 환전 없이<br>친구와 가볍게 즐기세요.</p></article></div>\n</section>\n<section id="lobby" class="lobby" hidden aria-labelledby="lobbyTitle"><div class="lobby-head"><div class="eyebrow">YOUR PRIVATE TABLE</div><h1 id="lobbyTitle">친구를 초대할 시간.</h1><p><span class="spinner" aria-hidden="true"></span>친구가 들어오면 첫 판이 시작됩니다.</p></div><div class="entry"><label class="label" for="inviteLink">이 링크를 친구 한 명에게 보내세요</label><div class="linkbox"><input type="text" id="inviteLink" readonly aria-label="친구 초대 링크"><button class="primary" id="copy" type="button">초대 링크 복사</button></div><p class="small">참가 권한이 포함된 비공개 링크입니다. 공개 게시판에는 올리지 마세요.</p><div class="lobby-members"><div class="member">PLAYER 01<b id="hostName">나</b><span>입장 완료</span></div><div class="member">PLAYER 02<b>친구 기다리는 중</b><span>초대 링크로 참가</span></div></div><div class="lobby-actions"><button id="share" class="green" type="button">다른 앱으로 링크 공유</button><button class="quiet" data-leave type="button">방 나가기</button></div></div></section>\n<section id="table" hidden aria-label="홀덤 테이블"><div class="gamehead"><div class="handtag" id="handInfo">HAND 01 · 프리플롭</div><div class="gamehead-actions"><button id="tableCopy" type="button" hidden>초대 링크</button><button data-leave type="button">방 나가기</button></div></div>\n<div class="table-area">\n<div class="seat opponent"><div class="player-info"><div class="playerrole">OPPONENT</div><div class="playername"><span id="opponentName">상대</span><span class="dealer" id="opponentDealer" hidden>D</span></div><div class="balance" id="opponentChips">1,000</div><div class="chipunit">PLAY CHIPS</div><div class="turn-badge" id="opponentTurn" hidden>상대 차례</div></div><div class="seat-cards" id="opponentCards" aria-label="상대 카드"></div><div class="stack-wrap" id="opponentStack" aria-hidden="true"></div></div>\n<div class="felt"><div class="pot"><span class="chip-stack" style="width:22px;height:22px" aria-hidden="true"><span class="chip gold" style="width:22px;height:22px;font-size:7px">P</span></span><span class="caption" id="potCaption">POT</span><strong id="pot">30</strong></div><div class="board" id="board" aria-label="공용 카드"></div><div class="betline"><span id="theirBet">상대 베팅 20</span><span id="myBet">내 베팅 10</span></div><div class="tablelog" id="tableLog" aria-live="polite"></div></div>\n<div class="seat my-seat"><div class="player-info"><div class="playerrole">YOU</div><div class="playername"><span id="myName">나</span><span class="dealer" id="myDealer" hidden>D</span></div><div class="balance" id="myChips">990</div><div class="chipunit">PLAY CHIPS</div><div class="turn-badge" id="myTurn" hidden>내 차례</div></div><div class="seat-cards" id="myCards" aria-label="내 카드"></div><div class="stack-wrap" id="myStack" aria-hidden="true"></div></div>\n</div>\n<div id="result" class="result" hidden><h2 id="resultTitle"></h2><p id="resultDetail"></p><p id="readyNote"></p><button class="primary" id="ready" type="button">다음 판 준비</button></div>\n<div class="actionpanel" id="actionPanel"><div class="action-title"><strong id="turnStatus" aria-live="polite">내 차례 · 행동을 선택하세요</strong><span class="hint">레이즈 금액은 이번 라운드의 총베팅액입니다.</span></div><div class="range-row"><button id="minus" aria-label="레이즈 10 줄이기" type="button">−</button><input type="range" min="40" max="1000" step="1" value="40" id="raiseRange" aria-label="레이즈 총액"><button id="plus" aria-label="레이즈 10 늘리기" type="button">+</button><output class="range-value" for="raiseRange" id="raiseValue">40</output></div><div class="presets"><button id="min" type="button">최소</button><button id="half" type="button">½ 팟</button><button id="potButton" type="button">팟</button></div><div class="bet-actions"><button id="fold" class="danger" type="button">폴드</button><button id="call" class="green" type="button">콜 10</button><button id="raise" class="primary" type="button">레이즈 40</button><button id="allin" type="button">올인</button></div></div>\n<div id="demoLabel" class="preview-note" hidden>화면 미리보기 · 실제 대전과 버튼 조작은 서버 배포 후 가능합니다.</div><p class="safety">PLAY MONEY ONLY · 실제 돈을 사용하지 않으며 결제·환전 기능이 없습니다.</p>\n</section>\n<section id="closed" class="lobby" hidden><div class="entry"><h2>방에 다시 연결할 수 없어요.</h2><p id="closedReason"></p><p class="small">같은 브라우저의 참가 정보로 돌아올 수 있지만, 서버가 재시작되거나 방을 나가면 기존 대전은 복구되지 않습니다.</p><button id="homeAgain" class="primary" type="button">새 방 만들러 가기</button></div></section>\n</main>\n<details id="help"><summary>사용 방법 · 연결이 끊겼을 때</summary><p>한 사람이 방을 만들고 초대 링크를 친구 한 명에게 보냅니다. 친구는 링크를 열고 닉네임을 입력합니다. 카드와 팟은 서버에서 관리하며 개인 카드는 각자에게만 전송됩니다. 자기 차례에 콜·레이즈·폴드·올인을 터치하세요. 한 판 후에는 두 사람 모두 준비를 눌러야 시작됩니다.</p><p>화면과 브라우저를 켜두세요. 25초 이상 응답이 없으면 상대 화면에 대기 안내가 뜨고, 양쪽이 돌아오면 재개됩니다. 다시 들어올 때는 <b>원래 사용한 기기의 같은 브라우저</b>를 사용하세요. 새로고침 시 재접속을 시도합니다. 시크릿 모드 종료나 사이트 데이터 삭제 후에는 자리를 되찾을 수 없습니다.</p><p>이 버전은 친구끼리 사용하는 소규모 시제품입니다. 방·칩은 서버 메모리에만 저장되므로 서버 재시작·절전·재배포 시 사라지며, 방은 마지막 활동 후 약 2시간이면 만료됩니다. 2인용이며 관전과 실제 결제는 없습니다.</p></details>\n<footer class="footer"><span>RIVER ROOM — 친구와 즐기는 작은 테이블.</span><span>2 PLAYERS · NO INSTALL · PLAY MONEY</span></footer>\n</div>\n<div class="toast" id="toast" hidden role="status"></div>\n<dialog id="leaveDialog" aria-labelledby="leaveTitle"><h2 id="leaveTitle">방을 나갈까요?</h2><p>상대와의 대전도 종료됩니다. 현재 칩과 진행 상황은 저장되지 않습니다.</p><div class="dialog-actions"><button id="cancelLeave" type="button">계속하기</button><button id="confirmLeave" class="danger" type="button">방 나가기</button></div></dialog>\n<script>\n\'use strict\';\nconst $=id=>document.getElementById(id);\nconst isFile=location.protocol===\'file:\';\nconst params=new URLSearchParams(location.hash.slice(1));\nlet invitation={room:params.get(\'room\')||\'\',invite:params.get(\'invite\')||\'\'};\nlet current=null,credential=\'\',roomId=invitation.room,busy=false,polling=false,timer=null,demo=false,raiseTarget=40,controlKey=\'\',toastTimer=null,networkDown=false,runId=0;\nconst storage={get(k){try{return localStorage.getItem(k)}catch{return null}},set(k,v){try{localStorage.setItem(k,v)}catch{}},remove(k){try{localStorage.removeItem(k)}catch{}}};\nfunction toast(text){$(\'toast\').textContent=text;$(\'toast\').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>$(\'toast\').hidden=true,5000)}\nfunction showPage(page){for(const id of [\'home\',\'lobby\',\'table\',\'closed\'])$(id).hidden=id!==page}\nfunction hideConnection(){$(\'connectionBanner\').hidden=true;networkDown=false;$(\'connectionTag\').classList.remove(\'offline\');$(\'tagText\').textContent=demo?\'화면 미리보기\':\'2인 프라이빗 홀덤\'}\nfunction connection(text){networkDown=true;$(\'connectionBanner\').hidden=false;$(\'connectionText\').textContent=text;$(\'connectionTag\').classList.add(\'offline\');$(\'tagText\').textContent=\'연결 확인 중\';if(current)render()}\nfunction readSaved(rid){try{return JSON.parse(storage.get(\'riverroom:\'+rid)||\'null\')}catch{return null}}\nfunction saveCredential(token,state){credential=token;roomId=state.room;storage.set(\'riverroom:\'+roomId,JSON.stringify({token,name:$(\'nickname\').value}));storage.set(\'riverroom:last\',roomId);}\nfunction inviteURL(){const cap=current?.invite||invitation.invite;if(!roomId||!cap)return \'\';return location.origin+location.pathname+\'#\'+new URLSearchParams({room:roomId,invite:cap}).toString()}\nasync function api(path,data=null,auth=true){const controller=new AbortController(),timeout=setTimeout(()=>controller.abort(),10000);try{const headers={\'Accept\':\'application/json\'};if(auth&&credential)headers.Authorization=\'Bearer \'+credential;const options={method:data?\'POST\':\'GET\',headers,cache:\'no-store\',signal:controller.signal,credentials:\'same-origin\'};if(data){headers[\'Content-Type\']=\'application/json\';options.body=JSON.stringify(data)}const response=await fetch(path,options);let result;try{result=await response.json()}catch{throw new Error(\'서버가 준비 중이거나 응답이 올바르지 않습니다. 잠시 후 다시 시도하세요.\')}if(!response.ok){const err=new Error(result.error||\'요청에 실패했습니다.\');err.status=response.status;throw err}return result}finally{clearTimeout(timeout)}}\nfunction apply(state){if(!state||state.room!==roomId)return;if(current&&state.revision<current.revision)return;current=state;render()}\nfunction stopPolling(){runId++;clearTimeout(timer);timer=null;polling=false}\nfunction beginPolling(){stopPolling();const epoch=runId;const tick=async()=>{if(epoch!==runId||!credential||!roomId||demo)return;await refresh(epoch);if(epoch===runId)timer=setTimeout(tick,900)};timer=setTimeout(tick,500)}\nasync function refresh(epoch=runId){if(polling||!credential||demo)return;polling=true;try{const answer=await api(\'/api/state?room=\'+encodeURIComponent(roomId));if(epoch!==runId)return;hideConnection();apply(answer.state)}catch(error){if(epoch!==runId)return;if([401,404,410].includes(error.status)){stopPolling();showPage(\'closed\');$(\'closedReason\').textContent=error.message;$(\'connectionBanner\').hidden=true}else{connection(\'연결을 확인하고 있습니다. 브라우저를 켜두면 다시 시도합니다.\')}}finally{if(epoch===runId)polling=false}}\nasync function enter(event){event.preventDefault();if(isFile){toast(\'실제 방 만들기는 서버를 배포한 웹 주소에서 이용하세요.\');return}if(busy)return;const name=$(\'nickname\').value.trim();if(!name)return;busy=true;$(\'enter\').disabled=true;try{let reply;if(invitation.room){reply=await api(\'/api/join\',{name,room:invitation.room,invite:invitation.invite},false)}else{reply=await api(\'/api/rooms\',{name},false)}saveCredential(reply.token,reply.state);roomId=reply.state.room;location.hash=new URLSearchParams({room:roomId,invite:reply.state.invite||invitation.invite}).toString();hideConnection();apply(reply.state);beginPolling()}catch(error){toast(error.name===\'AbortError\'?\'응답이 늦습니다. 잠시 후 다시 시도하세요.\':error.message)}finally{busy=false;$(\'enter\').disabled=false}}\nfunction makeCard(card,{empty=false,hidden=false,best=false}={}){const node=document.createElement(\'div\');node.className=\'card\';if(hidden){node.classList.add(\'back\');node.textContent=\'♠\';node.setAttribute(\'aria-label\',\'비공개 카드\');return node}if(empty||!card){node.classList.add(\'empty\');node.textContent=\'♠\';node.setAttribute(\'aria-label\',\'아직 공개되지 않은 카드\');return node}const [rank,suit]=card,face=({11:\'J\',12:\'Q\',13:\'K\',14:\'A\'})[rank]||String(rank),symbol=[\'♠\',\'♥\',\'♦\',\'♣\'][suit];if(suit===1||suit===2)node.classList.add(\'red\');if(best)node.classList.add(\'best\');const label=document.createElement(\'span\');label.className=\'rank\';label.textContent=face;node.append(label,document.createTextNode(symbol));const mini=document.createElement(\'span\');mini.className=\'mini\';mini.textContent=face;node.append(mini);node.setAttribute(\'aria-label\',symbol+\' \'+face);return node}\nfunction displayCards(id,cards,count,best=[]){const area=$(id);area.replaceChildren();for(let i=0;i<count;i++){const card=cards[i],marked=card&&best.some(c=>c[0]===card[0]&&c[1]===card[1]);area.append(makeCard(card,{empty:!card,hidden:id===\'opponentCards\'&&!card,best:marked}))}}\nfunction displayStack(id,amount){const parent=$(id);parent.replaceChildren();let remainder=amount;for(const [value,color] of [[500,\'gold\'],[100,\'blue\'],[25,\'\'],[5,\'red\']]){const n=Math.floor(remainder/value);remainder%=value;if(!n)continue;const stack=document.createElement(\'span\');stack.className=\'chip-stack\';for(const base of [true,false]){const chip=document.createElement(\'span\');chip.className=\'chip \'+color+(base?\' base\':\'\');chip.textContent=base?\'\':String(value);stack.append(chip)}parent.append(stack)}}\nfunction canPlay(){return !!current&&current.phase===\'betting\'&&current.turn===0&&!current.frozen&&!busy&&!demo&&!networkDown}\nfunction setTarget(value){if(!current?.legal)return;const o=current.legal,lo=Math.min(o.minimum,o.maximum);raiseTarget=Math.max(lo,Math.min(o.maximum,Math.round(value)));$(\'raiseRange\').value=String(raiseTarget);$(\'raiseValue\').textContent=raiseTarget.toLocaleString();$(\'raise\').textContent=\'레이즈 \'+raiseTarget.toLocaleString()}\nfunction render(){if(!current)return;const s=current;if(s.phase===\'lobby\'){showPage(\'lobby\');$(\'hostName\').textContent=s.players[0]?.name||\'나\';$(\'inviteLink\').value=inviteURL();return}showPage(\'table\');$(\'handInfo\').textContent=\'HAND \'+String(s.hand).padStart(2,\'0\')+\' · \'+s.stage;$(\'myName\').textContent=s.players[0].name;$(\'opponentName\').textContent=s.players[1].name;$(\'myChips\').textContent=s.chips[0].toLocaleString();$(\'opponentChips\').textContent=s.chips[1].toLocaleString();$(\'myDealer\').hidden=s.button!==0;$(\'opponentDealer\').hidden=s.button!==1;$(\'myTurn\').hidden=s.phase!==\'betting\'||s.turn!==0;$(\'opponentTurn\').hidden=s.phase!==\'betting\'||s.turn!==1;$(\'opponentTurn\').textContent=s.frozen?\'연결 대기\':\'상대 차례\';$(\'pot\').textContent=(s.phase===\'over\'?s.last_pot:s.pot).toLocaleString();$(\'potCaption\').textContent=s.phase===\'over\'?\'PAID\':\'POT\';$(\'theirBet\').textContent=\'상대 베팅 \'+s.bets[1];$(\'myBet\').textContent=\'내 베팅 \'+s.bets[0];$(\'tableLog\').textContent=s.frozen?\'상대 연결을 기다리고 있습니다. 양쪽 모두 화면을 켜주세요.\':s.log;const best=s.showdown?s.best_cards[s.winners[0]??0]||[]:[];displayCards(\'board\',s.board,5,best);displayCards(\'myCards\',s.holes[0],2,best);displayCards(\'opponentCards\',s.holes[1],2,best);displayStack(\'myStack\',s.chips[0]);displayStack(\'opponentStack\',s.chips[1]);$(\'tableCopy\').hidden=!s.invite||demo;$(\'result\').hidden=s.phase!==\'over\';$(\'actionPanel\').hidden=s.phase===\'over\';if(s.phase===\'over\'){$(\'resultTitle\').textContent=s.title;$(\'resultDetail\').textContent=s.detail;$(\'readyNote\').textContent=s.ready[1]?\'상대 준비 완료. 두 사람 모두 준비하면 시작합니다.\':\'두 사람 모두 준비하면 다음 대전이 시작됩니다.\';$(\'ready\').textContent=s.ready[0]?\'준비 완료 · 상대 기다리는 중\':Math.min(...s.chips)===0?\'새 대전 준비 · 1,000칩\':\'다음 판 준비\';$(\'ready\').disabled=s.ready[0]||s.frozen||busy||demo||networkDown}else{const o=s.legal,key=s.match+\':\'+s.hand+\':\'+s.revision;if(key!==controlKey){controlKey=key;raiseTarget=Math.min(o.minimum,o.maximum)}$(\'raiseRange\').min=String(Math.min(o.minimum,o.maximum));$(\'raiseRange\').max=String(o.maximum);setTarget(raiseTarget);const active=canPlay(),raising=active&&o.can_raise;for(const id of [\'raiseRange\',\'minus\',\'plus\',\'min\',\'half\',\'potButton\',\'raise\'])$(id).disabled=!raising;$(\'fold\').disabled=!active||o.call===0;$(\'call\').disabled=!active;$(\'allin\').disabled=!active||!o.can_allin;$(\'call\').textContent=o.call?\'콜 \'+Math.min(o.call,s.chips[0]).toLocaleString():\'체크\';$(\'turnStatus\').textContent=demo?\'미리보기 화면\':networkDown?\'연결 확인 중\':s.frozen?\'상대 연결 대기\':busy?\'입력 확인 중...\':s.phase===\'pause\'?\'다음 카드를 공개합니다...\':s.turn===0?\'내 차례 · 행동을 선택하세요\':\'상대가 행동을 선택하고 있습니다.\'}}\nfunction commandID(){if(globalThis.crypto?.getRandomValues){const bytes=new Uint8Array(16);crypto.getRandomValues(bytes);return Array.from(bytes,v=>v.toString(16).padStart(2,\'0\')).join(\'\')}return Date.now().toString(36)+\'_\'+Math.random().toString(36).slice(2)+\'_\'+Math.random().toString(36).slice(2)}\nasync function command(kind,action=\'\',target=0){if(busy||demo||networkDown||!current)return;const snapshot=current;busy=true;render();const epoch=runId;try{const result=await api(\'/api/action\',{room:roomId,match:snapshot.match,hand:snapshot.hand,revision:snapshot.revision,command:commandID(),kind,action,target});if(epoch===runId)apply(result.state)}catch(error){toast(error.name===\'AbortError\'?\'응답이 늦습니다. 결과를 확인 중입니다.\':error.message);await refresh(epoch)}finally{busy=false;if(epoch===runId&&current)render()}}\nasync function copyLink(){const link=inviteURL();if(!link){toast(\'초대 링크가 없습니다.\');return}try{if(!navigator.clipboard?.writeText)throw new Error(\'clipboard unavailable\');await navigator.clipboard.writeText(link);toast(\'초대 링크를 복사했습니다. 친구에게 보내세요.\')}catch{if($(\'lobby\').hidden){toast(\'아래 초대 링크를 길게 눌러 복사하세요.\');const box=document.createElement(\'input\');box.type=\'text\';box.value=link;box.readOnly=true;box.setAttribute(\'aria-label\',\'복사할 초대 링크\');$(\'tableCopy\').after(box);box.focus();box.select();setTimeout(()=>box.remove(),30000)}else{$(\'inviteLink\').focus();$(\'inviteLink\').select();toast(\'주소를 길게 누른 뒤 복사를 선택하세요.\')}}}\nasync function shareLink(){if(navigator.share){try{await navigator.share({title:\'리버룸 홀덤 초대\',text:\'같이 한 판 할까요? 링크를 열고 참가하세요.\',url:inviteURL()});return}catch(error){if(error.name===\'AbortError\')return}}await copyLink()}\nfunction resetHome(){stopPolling();current=null;credential=\'\';roomId=\'\';invitation={room:\'\',invite:\'\'};demo=false;busy=false;controlKey=\'\';history.replaceState(null,\'\',location.pathname+location.search);hideConnection();$(\'demoLabel\').hidden=true;$(\'homeTitle\').replaceChildren(document.createTextNode(\'링크 하나로,\'),document.createElement(\'br\'));const accent=document.createElement(\'span\');accent.className=\'accent\';accent.textContent=\'같은 테이블.\';$(\'homeTitle\').append(accent);$(\'homeIntro\').textContent=\'친구에게 초대 링크를 보내세요. 설치 없이 각자의 화면에서 즐기는 텍사스 홀덤.\';$(\'enter\').textContent=\'방 만들고 친구 초대하기 →\';$(\'enter\').disabled=isFile;showPage(\'home\')}\nasync function leave(){if(demo){resetHome();$(\'leaveDialog\').close();return}if(busy)return;busy=true;try{await api(\'/api/leave\',{room:roomId})}catch{toast(\'종료 요청이 전달되지 않았을 수 있습니다. 상대 화면에서 방 나가기를 눌러주세요.\')}finally{storage.remove(\'riverroom:\'+roomId);storage.remove(\'riverroom:last\');$(\'leaveDialog\').close();resetHome()}}\nfunction showDemo(){demo=true;roomId=\'preview\';current={room:roomId,match:\'preview\',revision:1,phase:\'betting\',frozen:false,ready:[false,false],players:[{name:\'플레이어\',online:true},{name:\'친구\',online:true}],invite:null,hand:3,stage:\'플롭\',turn:0,button:1,chips:[850,850],bets:[0,0],pot:300,last_pot:0,revealed:false,showdown:false,title:\'\',detail:\'\',note:\'\',log:\'플롭이 공개되었습니다.\',winners:[],board:[[14,1],[10,0],[7,2]],holes:[[[14,0],[13,0]],[null,null]],best_cards:[[],[]],legal:{call:0,maximum:850,minimum:20,can_raise:true,can_allin:true}};hideConnection();render();$(\'demoLabel\').hidden=false}\n$(\'entryForm\').addEventListener(\'submit\',enter);$(\'copy\').addEventListener(\'click\',copyLink);$(\'share\').addEventListener(\'click\',shareLink);$(\'tableCopy\').addEventListener(\'click\',copyLink);$(\'retry\').addEventListener(\'click\',()=>refresh());$(\'raiseRange\').addEventListener(\'input\',e=>setTarget(Number(e.target.value)));$(\'minus\').addEventListener(\'click\',()=>setTarget(raiseTarget-10));$(\'plus\').addEventListener(\'click\',()=>setTarget(raiseTarget+10));$(\'min\').addEventListener(\'click\',()=>setTarget(current.legal.minimum));$(\'half\').addEventListener(\'click\',()=>setTarget(Math.max(...current.bets)+Math.round((current.pot+current.legal.call)*.5)));$(\'potButton\').addEventListener(\'click\',()=>setTarget(Math.max(...current.bets)+current.pot+current.legal.call));$(\'fold\').addEventListener(\'click\',()=>command(\'act\',\'fold\'));$(\'call\').addEventListener(\'click\',()=>command(\'act\',\'call\'));$(\'raise\').addEventListener(\'click\',()=>command(\'act\',\'raise\',raiseTarget));$(\'allin\').addEventListener(\'click\',()=>command(\'act\',current.legal.can_raise?\'raise\':\'call\',current.legal.maximum));$(\'ready\').addEventListener(\'click\',()=>command(\'ready\'));for(const button of document.querySelectorAll(\'[data-leave]\'))button.addEventListener(\'click\',()=>$(\'leaveDialog\').showModal());$(\'cancelLeave\').addEventListener(\'click\',()=>$(\'leaveDialog\').close());$(\'confirmLeave\').addEventListener(\'click\',leave);$(\'homeAgain\').addEventListener(\'click\',()=>{storage.remove(\'riverroom:\'+roomId);resetHome()});$(\'preview\').addEventListener(\'click\',showDemo);document.addEventListener(\'visibilitychange\',()=>{if(!document.hidden&&credential)refresh()});window.addEventListener(\'online\',()=>{if(credential)refresh()});window.addEventListener(\'hashchange\',()=>{const p=new URLSearchParams(location.hash.slice(1));if(p.get(\'room\')&&p.get(\'room\')!==roomId){toast(\'다른 방 링크를 열었습니다. 페이지를 새로고침해 이동하세요.\')}});\n(async function init(){if(isFile){$(\'offlineNotice\').hidden=false;$(\'preview\').hidden=false;$(\'enter\').disabled=true;$(\'tagText\').textContent=\'화면 미리보기\';return}if(!roomId){const last=storage.get(\'riverroom:last\');if(last&&readSaved(last)){roomId=last;invitation.room=last}}if(roomId){const saved=readSaved(roomId);if(saved?.token){credential=saved.token;$(\'nickname\').value=saved.name||\'\';connection(\'기존 참가 정보로 방에 돌아가는 중입니다.\');await refresh();if(credential&&$(\'closed\').hidden)beginPolling();return}$(\'homeTitle\').textContent=\'친구의 테이블에 초대받았어요.\';$(\'homeIntro\').textContent=\'닉네임을 입력하고 참가하세요. 내 카드는 내 화면에만 표시됩니다.\';$(\'enter\').textContent=\'친구의 방에 참가하기 →\';if(!invitation.invite){$(\'enter\').disabled=true;toast(\'방 코드가 없는 링크입니다. 친구에게 전체 초대 링크를 다시 받아주세요.\')}}})();\n</script>\n</body>\n</html>\n'
ONLINE_GRACE = 25.0
ROOM_TTL = 7200.0
MAX_ROOMS = 100
MAX_BODY = 4096


class ApiError(Exception):
    def __init__(self, message, status=400):
        self.message, self.status = message, status
        super().__init__(message)


def clean_name(value):
    if not isinstance(value, str):
        raise ApiError('닉네임을 입력하세요.')
    value = unicodedata.normalize('NFC', value)
    value = ''.join(c for c in value if not unicodedata.category(c).startswith('C'))
    value = ' '.join(value.split())[:16]
    if not value:
        raise ApiError('닉네임을 입력하세요.')
    return value


def safe_equal(a, b):
    return (isinstance(a, str) and isinstance(b, str) and a.isascii()
            and b.isascii() and hmac.compare_digest(a, b))


class Player:
    def __init__(self, name, now):
        self.name = name
        self.token = secrets.token_urlsafe(32)
        self.last_seen = now
        self.commands = OrderedDict()


class Room:
    def __init__(self, name, now):
        self.id = secrets.token_urlsafe(12)
        self.invite = secrets.token_urlsafe(24)
        self.players = [Player(name, now), None]
        self.game = None
        self.match = secrets.token_urlsafe(12)
        self.revision = 1
        self.ready = [False, False]
        self.frozen = False
        self.closed = False
        self.last_access = now

    def authenticate(self, token):
        for seat, player in enumerate(self.players):
            if player and safe_equal(player.token, token):
                return seat
        raise ApiError('이 방의 참가 정보가 없습니다. 원래 브라우저로 열어주세요.', 401)

    def online(self, now):
        return [bool(p and now - p.last_seen <= ONLINE_GRACE) for p in self.players]

    def visit(self, seat, now):
        if self.closed:
            raise ApiError('참가자가 방을 나갔습니다. 새 방을 만들어주세요.', 410)
        # Observe an offline interval before refreshing the caller's heartbeat.
        before = bool(self.game and not all(self.online(now)))
        self.players[seat].last_seen = now
        self.last_access = now
        frozen = bool(self.game and not all(self.online(now)))
        was_frozen = self.frozen or before
        if frozen != self.frozen or (was_frozen and not frozen):
            self.revision += 1
        self.frozen = frozen
        if self.game and not frozen:
            if was_frozen and self.game.phase == 'pause':
                self.game.deadline = now + .9
            if self.game.advance(now):
                self.revision += 1

    def join(self, invite, name, now):
        if self.closed or not safe_equal(self.invite, invite):
            raise ApiError('초대 링크가 올바르지 않거나 만료되었습니다.', 404)
        if self.players[1] is not None:
            raise ApiError('이미 2명이 참가한 방입니다. 기존 참가자는 원래 브라우저로 다시 열어주세요.', 409)
        self.players[1] = Player(name, now)
        self.last_access = now
        self.game = Game([p.name for p in self.players])
        self.revision += 1
        self.frozen = not all(self.online(now))
        return 1

    def view(self, seat, now):
        order = [seat, 1-seat]
        flip = lambda i: None if i is None else order.index(i)
        g = self.game
        online = self.online(now)
        result = dict(room=self.id, match=self.match, revision=self.revision,
                      phase='lobby' if g is None else g.phase, frozen=self.frozen,
                      ready=[self.ready[i] for i in order],
                      players=[None if self.players[i] is None else
                               {'name': self.players[i].name, 'online': online[i]} for i in order],
                      # Only the host gets the invitation capability.
                      invite=self.invite if seat == 0 else None,
                      hand=0 if g is None else g.hand_no)
        if g is None:
            return result
        result.update(stage=g.stage_name, turn=flip(g.turn), button=flip(g.button),
                      chips=[g.chips[i] for i in order], bets=[g.bets[i] for i in order],
                      pot=g.pot, last_pot=g.last_pot, revealed=g.revealed,
                      showdown=g.showdown, title=g.title, detail=g.detail,
                      note=g.note, log=g.log, winners=[flip(i) for i in g.winners],
                      board=[list(c) for c in g.board],
                      holes=[[list(c) for c in g.holes[i]] if i == seat or g.revealed
                             else [None, None] for i in order],
                      best_cards=[[list(c) for c in g.best_cards[i]] if i == seat or g.revealed
                                  else [] for i in order],
                      legal=g.legal(seat))
        return result

    def command(self, seat, data, now):
        self.visit(seat, now)
        cmd_id = data.get('command')
        if not isinstance(cmd_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{12,64}', cmd_id):
            raise ApiError('입력 식별자가 올바르지 않습니다.')
        player = self.players[seat]
        if cmd_id in player.commands:
            return self.view(seat, now)
        g = self.game
        if not g:
            raise ApiError('상대가 참가할 때까지 기다려주세요.', 409)
        if self.frozen:
            raise ApiError('상대 연결을 기다리고 있습니다. 상대가 돌아오면 재개됩니다.', 409)
        if (data.get('match') != self.match or type(data.get('hand')) is not int
                or data['hand'] != g.hand_no):
            raise ApiError('판이 바뀌었습니다. 화면을 갱신해 다시 눌러주세요.', 409)
        if data.get('kind') == 'ready':
            if g.phase != 'over' or self.ready[seat]:
                raise ApiError('지금은 준비할 수 없습니다.', 409)
            self.ready[seat] = True
            if all(self.ready):
                if min(g.chips) == 0:
                    self.game = Game([p.name for p in self.players])
                    self.match = secrets.token_urlsafe(12)
                else:
                    g.start_hand(now)
                self.ready = [False, False]
        elif data.get('kind') == 'act':
            if type(data.get('revision')) is not int or data['revision'] != self.revision:
                raise ApiError('상태가 갱신되었습니다. 다시 눌러주세요.', 409)
            action = data.get('action')
            target = data.get('target', 0)
            if (action not in ('call', 'raise', 'fold') or type(target) is not int
                    or not 0 <= target <= START_CHIPS*2):
                raise ApiError('허용되지 않는 베팅입니다.')
            if not g.act(seat, action, target, now):
                raise ApiError('차례와 최소 베팅 금액을 확인하세요.', 409)
        else:
            raise ApiError('알 수 없는 입력입니다.')
        player.commands[cmd_id] = True
        while len(player.commands) > 128:
            player.commands.popitem(last=False)
        self.revision += 1
        return self.view(seat, now)


class Service:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.rooms = {}
        self.lock = threading.RLock()
        self.last_sweep = 0.0
        self.limits = OrderedDict()

    def cleanup(self, now):
        if now - self.last_sweep < 60:
            return
        self.last_sweep = now
        for rid in list(self.rooms):
            r = self.rooms[rid]
            if now - r.last_access > ROOM_TTL or r.closed:
                del self.rooms[rid]
        for key in list(self.limits):
            if not self.limits[key] or now - self.limits[key][-1] > 60:
                del self.limits[key]

    def limit(self, key, count, now):
        if key not in self.limits and len(self.limits) >= 2000:
            self.limits.popitem(last=False)
        q = self.limits.setdefault(key, deque())
        while q and now - q[0] >= 60:
            q.popleft()
        if len(q) >= count:
            raise ApiError('요청이 너무 많습니다. 잠시 후 다시 시도하세요.', 429)
        q.append(now)
        self.limits.move_to_end(key)

    def find(self, rid):
        if not isinstance(rid, str) or rid not in self.rooms:
            raise ApiError('방을 찾을 수 없습니다. 서버가 재시작되었거나 방이 만료되었을 수 있습니다.', 404)
        return self.rooms[rid]

    def dispatch(self, method, path, data, token='', client=''):
        now = self.clock()
        with self.lock:
            self.cleanup(now)
            if path == '/api/rooms' and method == 'POST':
                self.limit(('create', client), 8, now)
                if len(self.rooms) >= MAX_ROOMS:
                    raise ApiError('서버의 방이 가득 찼습니다. 잠시 후 시도하세요.', 503)
                room = Room(clean_name(data.get('name')), now)
                self.rooms[room.id] = room
                return dict(token=room.players[0].token, state=room.view(0, now))
            if path == '/api/join' and method == 'POST':
                self.limit(('join', client), 25, now)
                room = self.find(data.get('room'))
                seat = room.join(data.get('invite'), clean_name(data.get('name')), now)
                return dict(token=room.players[seat].token, state=room.view(seat, now))
            if path in ('/api/state', '/api/action', '/api/leave'):
                room = self.find(data.get('room'))
                seat = room.authenticate(token)
                self.limit(('player', room.id, seat), 200, now)
                if path == '/api/state' and method == 'GET':
                    room.visit(seat, now)
                    return {'state': room.view(seat, now)}
                if path == '/api/action' and method == 'POST':
                    return {'state': room.command(seat, data, now)}
                if path == '/api/leave' and method == 'POST':
                    room.closed = True
                    return {'ok': True}
            raise ApiError('요청한 주소를 찾을 수 없습니다.', 404)


STATUS = {200: 'OK', 400: 'Bad Request', 401: 'Unauthorized', 403: 'Forbidden',
          404: 'Not Found', 405: 'Method Not Allowed', 409: 'Conflict',
          410: 'Gone', 413: 'Payload Too Large', 429: 'Too Many Requests',
          500: 'Internal Server Error', 503: 'Service Unavailable'}


def create_app(service=None):
    service = service or Service()
    html = HTML_PAGE.encode('utf-8')

    def app(environ, start_response):
        status = 200
        content_type = 'application/json; charset=utf-8'
        method = environ.get('REQUEST_METHOD', 'GET')
        path = environ.get('PATH_INFO', '/')
        try:
            if method in ('GET', 'HEAD') and path == '/':
                content_type = 'text/html; charset=utf-8'
                body = html
            elif method in ('GET', 'HEAD') and path == '/healthz':
                body = b'{"ok":true}'
            elif method == 'GET' and path == '/robots.txt':
                content_type = 'text/plain; charset=utf-8'
                body = b'User-agent: *\nDisallow: /\n'
            elif method == 'GET' and path == '/favicon.ico':
                content_type = 'image/svg+xml'
                body = b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64"><rect width="64" height="64" rx="16" fill="#102924"/><text x="32" y="47" text-anchor="middle" font-size="46" fill="#e5bd78">&#9824;</text></svg>'
            else:
                if method not in ('GET', 'POST'):
                    raise ApiError('허용되지 않는 방식입니다.', 405)
                origin = environ.get('HTTP_ORIGIN', '')
                if origin and (urlsplit(origin).scheme not in ('http', 'https')
                               or urlsplit(origin).netloc.lower() != environ.get('HTTP_HOST', '').lower()):
                    raise ApiError('다른 사이트에서 보낸 요청은 허용하지 않습니다.', 403)
                if environ.get('HTTP_SEC_FETCH_SITE') == 'cross-site':
                    raise ApiError('같은 사이트에서 접속하세요.', 403)
                if method == 'POST':
                    try:
                        length = int(environ.get('CONTENT_LENGTH') or '0')
                    except ValueError:
                        raise ApiError('잘못된 요청 크기입니다.')
                    if not 0 < length <= MAX_BODY:
                        raise ApiError('요청 크기가 너무 크거나 비어 있습니다.', 413)
                    if not environ.get('CONTENT_TYPE', '').lower().startswith('application/json'):
                        raise ApiError('JSON 요청만 허용합니다.', 400)
                    try:
                        data = json.loads(environ['wsgi.input'].read(length).decode('utf-8'))
                    except (ValueError, UnicodeError, RecursionError):
                        raise ApiError('잘못된 JSON 형식입니다.')
                    if not isinstance(data, dict):
                        raise ApiError('요청 형식을 확인하세요.')
                else:
                    query = parse_qs(environ.get('QUERY_STRING', ''), max_num_fields=8)
                    data = {key: values[-1] for key, values in query.items()}
                auth = environ.get('HTTP_AUTHORIZATION', '')
                token = auth[7:] if auth.startswith('Bearer ') else ''
                if len(token) > 100:
                    raise ApiError('인증 정보를 확인하세요.', 401)
                result = service.dispatch(method, path, data, token,
                                          client=environ.get('REMOTE_ADDR', 'unknown'))
                body = json.dumps(result, ensure_ascii=False, allow_nan=False).encode('utf-8')
        except ApiError as error:
            status = error.status
            body = json.dumps({'error': error.message}, ensure_ascii=False).encode('utf-8')
        except (ValueError, TypeError, KeyError):
            status = 400
            body = '{"error":"잘못된 요청입니다."}'.encode('utf-8')
        except Exception:
            status = 500
            body = '{"error":"서버 오류가 발생했습니다. 잠시 후 다시 시도하세요."}'.encode('utf-8')
        headers = [('Content-Type', content_type), ('Content-Length', str(len(body))),
                   ('Cache-Control', 'no-store'), ('X-Content-Type-Options', 'nosniff'),
                   ('Referrer-Policy', 'no-referrer'), ('X-Frame-Options', 'DENY'),
                   ('Permissions-Policy', 'camera=(), microphone=(), geolocation=()'),
                   ('Content-Security-Policy', "default-src 'self'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; img-src 'self' data:; font-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
        start_response(f'{status} {STATUS[status]}', headers)
        return [b'' if method == 'HEAD' else body]

    return app


# Importing initializes memory only. It does NOT run a listener or a background job.
# All game assets are included in this single file.
app = create_app()


def main():
    from socketserver import ThreadingMixIn
    from wsgiref.simple_server import WSGIServer, WSGIRequestHandler, make_server

    class ThreadedServer(ThreadingMixIn, WSGIServer):
        daemon_threads = True
        request_queue_size = 32

    class QuietHandler(WSGIRequestHandler):
        def log_message(self, *args):
            pass

    port = int(os.environ.get('PORT', '8000'))
    print('리버룸 · 단일 파일 웹 홀덤')
    print('잠시 후 서버 시작 완료가 뜨면 브라우저를 여세요.')
    print(f'이 탭에서 접속: http://127.0.0.1:{port}')
    print('인터넷 주소창에 입력하세요. 검색창이 아닙니다.')
    print('Pydroid는 실행 중인 상태로 두세요. 아래 화면이 멈춰 보여도 정상입니다.')
    print('같은 Wi-Fi 친구: 방장 탭의 Wi-Fi IPv4 주소와 위 포트로 접속하세요.')
    print('친구에게 보낼 링크를 만들 때 방장도 Wi-Fi IPv4 주소로 접속하세요.')
    print('127.0.0.1 주소는 다른 기기에서 접속할 수 없습니다.')
    print('다른 장소의 친구 초대는 별도 HTTPS 서버 배포가 필요합니다.')
    print('종료하면 모든 방이 사라집니다. 중단: Ctrl+C')
    with make_server('0.0.0.0', port, app, ThreadedServer, QuietHandler) as httpd:
        print('서버 시작 완료 · 브라우저로 이동하세요.', flush=True)
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print('\n서버 종료')


if __name__ == '__main__':
    main()
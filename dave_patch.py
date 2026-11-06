"""
Discord Audio/Video End-to-End Encryption (DAVE) Protocol Runtime Patch for discord.py 2.7.x

[문제 배경]
최근 Discord는 음성 채널에 최신 DAVE (Audio E2EE) 프로토콜을 의무화하기 시작했습니다.
(미지원 시 WebSocket Close 4017: DAVE protocol required 발생)
discord.py 2.7.x는 davey 패키지가 설치되어 있으면 DAVE 프로토콜 협상을 시도하지만,
음성 게이트웨이로부터 MLS_ANNOUNCE_COMMIT_TRANSITION, MLS_WELCOME,
DAVE_PREPARE_TRANSITION, DAVE_EXECUTE_TRANSITION 등을 수신했을 때
davey.DaveSession의 prepare_transition() 및 execute_transition()을 호출하여
상태를 전이(Transition)시키는 로직이 미완성(누락)되어 있습니다.

이로 인해 VoiceConnectionState의 상태가 영구적으로 ready=False, can_encrypt=False (PENDING)로 묶여,
봇은 DAVE 채널에 암호화되지 않은 플레인 Opus 패킷을 전송하게 됩니다.
디스코드 공식 앱(데스크톱, 모바일, 웹)은 규격에 맞지 않는 비암호화 패킷을 전량 폐기(Drop)하므로,
봇은 정상 재생 중으로 표시되지만 사용자에게는 완벽한 무음(묵음)으로 처리됩니다.

[해결책]
본 모듈은 discord.py의 DiscordVoiceWebSocket 및 VoiceConnectionState를 런타임에 안전하게 패치하여:
1. 게이트웨이의 MLS Commit/Welcome 및 Transition 신호 수신 시 prepare_transition/execute_transition을 호출합니다.
2. can_encrypt 상태를 True로 정상 활성화하여 모든 Opus 오디오 프레임이 DAVE 규격에 맞게 E2EE 암호화되도록 보장합니다.
3. DAVE 프로토콜이 비활성화된 서버나 구형 환경에서도 호환성을 유지합니다.
"""

import struct
import logging
from typing import Any, Dict

logger = logging.getLogger("discord.dave_patch")


def apply_dave_patch() -> bool:
    """
    discord.py 음성 연결에 DAVE E2EE 상태 머신 패치를 적용합니다.
    이미 적용되었거나 davey 모듈이 없는 경우 안전하게 무시됩니다.
    """
    try:
        import davey
        from discord.gateway import DiscordVoiceWebSocket
        from discord.voice_state import VoiceConnectionState
    except ImportError:
        logger.warning("⚠️ [DAVE Patch] 'davey' 또는 discord voice 모듈이 없습니다. (pip install davey>=0.1.6 권장)")
        return False

    if getattr(VoiceConnectionState, "_dave_patched", False):
        return True

    orig_execute_transition = VoiceConnectionState._execute_transition
    orig_received_message = DiscordVoiceWebSocket.received_message
    orig_received_binary_message = DiscordVoiceWebSocket.received_binary_message

    async def patched_execute_transition(self: VoiceConnectionState, transition_id: int) -> None:
        logger.debug(f"[DAVE] Executing transition id {transition_id}")
        if transition_id not in self.dave_pending_transitions and transition_id != 0:
            logger.warning(f"[DAVE] Received execute transition, but no pending transition for id {transition_id}")
            return

        old_version = self.dave_protocol_version
        if transition_id in self.dave_pending_transitions:
            self.dave_protocol_version = self.dave_pending_transitions.pop(transition_id)

        if self.dave_session is not None:
            try:
                res = self.dave_session.execute_transition(transition_id)
                logger.info(
                    f"[DAVE] dave_session.execute_transition({transition_id}) -> {res} | "
                    f"status={self.dave_session.status}, ready={self.dave_session.ready}, can_encrypt={self.can_encrypt}"
                )
            except Exception as e:
                logger.debug(f"[DAVE] execute_transition({transition_id}) exception: {e}")

        if old_version != self.dave_protocol_version and self.dave_protocol_version == 0:
            self.dave_downgraded = True
            logger.info("[DAVE] DAVE Session downgraded to v0")
        elif transition_id > 0 and self.dave_downgraded:
            self.dave_downgraded = False
            if self.dave_session is not None:
                self.dave_session.set_passthrough_mode(True, 10)
            logger.info("[DAVE] DAVE Session upgraded")

        logger.debug(f"[DAVE] Transition id {transition_id} executed successfully")

    VoiceConnectionState._execute_transition = patched_execute_transition

    async def patched_received_binary_message(self: DiscordVoiceWebSocket, msg: bytes) -> None:
        self.seq_ack = struct.unpack_from('>H', msg, 0)[0]
        op = msg[2]
        state = self._connection
        if state.dave_session is None:
            return

        if op == self.MLS_EXTERNAL_SENDER:
            logger.info("[DAVE] Voice WS binary: MLS_EXTERNAL_SENDER")
            try:
                state.dave_session.set_external_sender(msg[3:])
            except Exception as e:
                logger.error(f"[DAVE] set_external_sender failed: {e}")

        elif op == self.MLS_PROPOSALS:
            logger.info("[DAVE] Voice WS binary: MLS_PROPOSALS")
            optype = msg[3]
            try:
                result = state.dave_session.process_proposals(
                    davey.ProposalsOperationType.append if optype == 0 else davey.ProposalsOperationType.revoke,
                    msg[4:]
                )
                if isinstance(result, davey.CommitWelcome):
                    await self.send_binary(
                        DiscordVoiceWebSocket.MLS_COMMIT_WELCOME,
                        result.commit + result.welcome if result.welcome else result.commit,
                    )
                    logger.info("[DAVE] Sent MLS_COMMIT_WELCOME")
            except Exception as e:
                logger.error(f"[DAVE] process_proposals failed: {e}")

        elif op == self.MLS_ANNOUNCE_COMMIT_TRANSITION:
            transition_id = struct.unpack_from('>H', msg, 3)[0]
            logger.info(f"[DAVE] Voice WS binary: MLS_ANNOUNCE_COMMIT_TRANSITION (id={transition_id})")
            try:
                should_prepare = state.dave_session.process_commit(msg[5:])
                if transition_id != 0:
                    state.dave_pending_transitions[transition_id] = state.dave_protocol_version
                    if should_prepare:
                        try:
                            state.dave_session.prepare_transition(transition_id, state.dave_protocol_version)
                        except Exception as pe:
                            logger.debug(f"[DAVE] prepare_transition({transition_id}) err: {pe}")
                    await self.send_transition_ready(transition_id)
                else:
                    if should_prepare:
                        try:
                            state.dave_session.prepare_transition(0, state.dave_protocol_version)
                            state.dave_session.execute_transition(0)
                            logger.info(
                                f"[DAVE] Transition 0 ready: ready={state.dave_session.ready}, can_encrypt={state.can_encrypt}"
                            )
                        except Exception as pe:
                            logger.debug(f"[DAVE] prepare/execute transition 0 err: {pe}")
            except Exception as e:
                logger.error(f"[DAVE] MLS commit failed: {e}")
                await state._recover_from_invalid_commit(transition_id)

        elif op == self.MLS_WELCOME:
            transition_id = struct.unpack_from('>H', msg, 3)[0]
            logger.info(f"[DAVE] Voice WS binary: MLS_WELCOME (id={transition_id})")
            try:
                state.dave_session.process_welcome(msg[5:])
                if transition_id != 0:
                    state.dave_pending_transitions[transition_id] = state.dave_protocol_version
                    try:
                        state.dave_session.prepare_transition(transition_id, state.dave_protocol_version)
                    except Exception as pe:
                        logger.debug(f"[DAVE] prepare_transition({transition_id}) err: {pe}")
                    await self.send_transition_ready(transition_id)
                else:
                    try:
                        state.dave_session.prepare_transition(0, state.dave_protocol_version)
                        state.dave_session.execute_transition(0)
                        logger.info(
                            f"[DAVE] Welcome transition 0 ready: ready={state.dave_session.ready}, can_encrypt={state.can_encrypt}"
                        )
                    except Exception as pe:
                        logger.debug(f"[DAVE] welcome prepare/execute transition 0 err: {pe}")
            except Exception as e:
                logger.error(f"[DAVE] MLS welcome failed: {e}")
                await state._recover_from_invalid_commit(transition_id)

    DiscordVoiceWebSocket.received_binary_message = patched_received_binary_message

    async def patched_received_message(self: DiscordVoiceWebSocket, msg: Dict[str, Any]) -> None:
        op = msg.get('op')
        data = msg.get('d')
        state = self._connection
        if state and state.dave_session:
            if op == self.DAVE_PREPARE_TRANSITION:
                t_id = data['transition_id']
                p_ver = data['protocol_version']
                logger.info(f"[DAVE] Voice WS text: DAVE_PREPARE_TRANSITION (id={t_id}, ver={p_ver})")
                state.dave_pending_transitions[t_id] = p_ver
                if t_id != 0:
                    try:
                        state.dave_session.prepare_transition(t_id, p_ver)
                    except Exception as e:
                        logger.debug(f"[DAVE] prepare_transition({t_id}) err: {e}")
                if t_id == 0:
                    await state._execute_transition(t_id)
                else:
                    if p_ver == 0 and state.dave_session:
                        state.dave_session.set_passthrough_mode(True, 120)
                    await self.send_transition_ready(t_id)
                return

            elif op == self.DAVE_EXECUTE_TRANSITION:
                t_id = data['transition_id']
                logger.info(f"[DAVE] Voice WS text: DAVE_EXECUTE_TRANSITION (id={t_id})")
                await state._execute_transition(t_id)
                return

        await orig_received_message(self, msg)

    DiscordVoiceWebSocket.received_message = patched_received_message
    VoiceConnectionState._dave_patched = True
    print("  🛡️ [DAVE Voice Patch] discord.py DAVE E2EE 음성 암호화 패치가 성공적으로 적용되었습니다!")
    return True

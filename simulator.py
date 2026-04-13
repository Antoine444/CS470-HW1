#!/usr/bin/env python3
import json
import sys
import copy
from collections import deque

MASK = (1 << 64) - 1


def parse_instructions(program_json):
    instructions = []
    for s in program_json:
        parts = s.replace(",", " ").split()
        opcode = parts[0]
        dest = int(parts[1][1:])  # xN -> N
        src1 = int(parts[2][1:])  # xN -> N
        if opcode == "addi":
            imm = int(parts[3]) & MASK  # sign-extend to 64-bit unsigned
            instructions.append({"opcode": opcode, "dest": dest, "src1": src1, "imm": imm})
        else:
            src2 = int(parts[3][1:])
            instructions.append({"opcode": opcode, "dest": dest, "src1": src1, "src2": src2})
    return instructions


def initial_state():
    return {
        "pc": 0,
        "decoded_pcs": [],
        "rmt": list(range(32)),
        "free_list": deque(range(32, 64)),
        "bbt": [False] * 64,
        "active_list": [],
        "integer_queue": [],
        "prf": [0] * 64,
        "exception": False,
        "exception_pc": 0,
        "alu_stage1": [],
        "alu_stage2": [],
    }


def dump_state(state):
    return {
        "ActiveList": [
            {
                "Done": e["Done"],
                "Exception": e["Exception"],
                "LogicalDestination": e["LogicalDestination"],
                "OldDestination": e["OldDestination"],
                "PC": e["PC"],
            }
            for e in state["active_list"]
        ],
        "BusyBitTable": list(state["bbt"]),
        "DecodedPCs": list(state["decoded_pcs"]),
        "Exception": state["exception"],
        "ExceptionPC": state["exception_pc"],
        "FreeList": list(state["free_list"]),
        "IntegerQueue": [
            {
                "DestRegister": e["DestRegister"],
                "OpAIsReady": e["OpAIsReady"],
                "OpARegTag": e["OpARegTag"],
                "OpAValue": e["OpAValue"],
                "OpBIsReady": e["OpBIsReady"],
                "OpBRegTag": e["OpBRegTag"],
                "OpBValue": e["OpBValue"],
                "OpCode": e["OpCode"],
                "PC": e["PC"],
            }
            for e in state["integer_queue"]
        ],
        "PC": state["pc"],
        "PhysicalRegisterFile": list(state["prf"]),
        "RegisterMapTable": list(state["rmt"]),
    }


def compute_result(opcode, val_a, val_b):
    """Returns (result, has_exception)."""
    if opcode in ("add", "addi"):
        return (val_a + val_b) & MASK, False
    elif opcode == "sub":
        return (val_a - val_b) & MASK, False
    elif opcode == "mulu":
        return (val_a * val_b) & MASK, False
    elif opcode == "divu":
        if val_b == 0:
            return 0, True
        return (val_a // val_b) & MASK, False
    elif opcode == "remu":
        if val_b == 0:
            return 0, True
        return (val_a % val_b) & MASK, False
    return 0, False


def commit(cur, nxt):
    if cur["exception"]:
        # Exception mode: roll back from tail
        if not cur["active_list"]:
            nxt["exception"] = False
        else:
            count = min(4, len(nxt["active_list"]))
            for _ in range(count):
                entry = nxt["active_list"].pop()  # pop from tail (newest)
                log_dest = entry["LogicalDestination"]
                new_phys = nxt["rmt"][log_dest]
                nxt["rmt"][log_dest] = entry["OldDestination"]
                nxt["free_list"].append(new_phys)
                nxt["bbt"][new_phys] = False
    else:
        # Normal mode: retire from head
        for idx in range(4):
            if idx >= len(cur["active_list"]):
                break
            entry = cur["active_list"][idx]
            if not entry["Done"]:
                break
            if entry["Exception"]:
                # Exception detected
                nxt["exception"] = True
                nxt["exception_pc"] = entry["PC"]
                nxt["integer_queue"] = []
                nxt["alu_stage1"] = []
                nxt["alu_stage2"] = []
                # Do NOT retire the faulting instruction
                break
            # Retire: pop from head of nxt active list
            retired = nxt["active_list"].pop(0)
            nxt["free_list"].append(retired["OldDestination"])


def alu_forward(cur, nxt):
    if nxt["exception"]:
        return
    for alu_entry in cur["alu_stage2"]:
        dest = alu_entry["dest"]
        opcode = alu_entry["opcode"]
        val_a = alu_entry["val_a"]
        val_b = alu_entry["val_b"]
        pc = alu_entry["pc"]

        result, has_exception = compute_result(opcode, val_a, val_b)

        # Update Active List: find by PC
        for al_entry in nxt["active_list"]:
            if al_entry["PC"] == pc:
                al_entry["Done"] = True
                al_entry["Exception"] = has_exception
                break

        if not has_exception:
            # Write PRF, clear busy bit, wake IQ
            nxt["prf"][dest] = result
            nxt["bbt"][dest] = False
            for iq_entry in nxt["integer_queue"]:
                if not iq_entry["OpAIsReady"] and iq_entry["OpARegTag"] == dest:
                    iq_entry["OpAIsReady"] = True
                    iq_entry["OpAValue"] = result
                if not iq_entry["OpBIsReady"] and iq_entry["OpBRegTag"] == dest:
                    iq_entry["OpBIsReady"] = True
                    iq_entry["OpBValue"] = result


def alu_advance(cur, nxt):
    if nxt["exception"]:
        return
    nxt["alu_stage2"] = list(cur["alu_stage1"])
    nxt["alu_stage1"] = []


def issue(cur, nxt):
    if nxt["exception"]:
        return
    # Find ready instructions in nxt IQ
    ready = [e for e in nxt["integer_queue"] if e["OpAIsReady"] and e["OpBIsReady"]]
    # Sort by PC (oldest first), pick up to 4
    ready.sort(key=lambda e: e["PC"])
    to_issue = ready[:4]

    for e in to_issue:
        nxt["integer_queue"].remove(e)
        nxt["alu_stage1"].append({
            "dest": e["DestRegister"],
            "opcode": e["OpCode"],
            "val_a": e["OpAValue"],
            "val_b": e["OpBValue"],
            "pc": e["PC"],
        })


def rename_dispatch(cur, nxt, program):
    if nxt["exception"]:
        return True  # no backpressure signal needed
    decoded = cur["decoded_pcs"]
    if not decoded:
        return True  # nothing to dispatch, no backpressure

    n = len(decoded)
    # Check resources in NEXT state
    if (len(nxt["free_list"]) < n or
            len(nxt["active_list"]) + n > 32 or
            len(nxt["integer_queue"]) + n > 32):
        return False  # backpressure

    for pc in decoded:
        instr = program[pc]
        # Allocate physical register
        new_phys = nxt["free_list"].popleft()

        # Read source tags BEFORE renaming dest (matters when dest == src)
        tag_a = nxt["rmt"][instr["src1"]]
        if instr["opcode"] != "addi":
            tag_b = nxt["rmt"][instr["src2"]]

        # Rename destination
        old_dest = nxt["rmt"][instr["dest"]]
        nxt["rmt"][instr["dest"]] = new_phys
        nxt["bbt"][new_phys] = True

        # Read OpA readiness (using pre-captured tag)
        if nxt["bbt"][tag_a]:
            ready_a = False
            val_a = 0
        else:
            ready_a = True
            val_a = nxt["prf"][tag_a]

        # Read OpB
        if instr["opcode"] == "addi":
            ready_b = True
            tag_b = 0
            val_b = instr["imm"]
        else:
            if nxt["bbt"][tag_b]:
                ready_b = False
                val_b = 0
            else:
                ready_b = True
                val_b = nxt["prf"][tag_b]

        # OpCode: addi -> add
        opcode = "add" if instr["opcode"] == "addi" else instr["opcode"]

        # Add IQ entry
        nxt["integer_queue"].append({
            "DestRegister": new_phys,
            "OpAIsReady": ready_a,
            "OpARegTag": tag_a,
            "OpAValue": val_a,
            "OpBIsReady": ready_b,
            "OpBRegTag": tag_b,
            "OpBValue": val_b,
            "OpCode": opcode,
            "PC": pc,
        })

        # Add AL entry
        nxt["active_list"].append({
            "Done": False,
            "Exception": False,
            "LogicalDestination": instr["dest"],
            "OldDestination": old_dest,
            "PC": pc,
        })

    return True  # dispatched


def fetch_decode(cur, nxt, program, dispatched):
    if nxt["exception"]:
        nxt["decoded_pcs"] = []
        nxt["pc"] = 0x10000
        return

    if not dispatched and cur["decoded_pcs"]:
        # Backpressure: keep current decoded PCs and PC
        nxt["decoded_pcs"] = list(cur["decoded_pcs"])
        nxt["pc"] = cur["pc"]
        return

    # Fetch up to 4 new instructions
    fetch_pc = cur["pc"]
    num_fetch = min(4, len(program) - fetch_pc)
    if num_fetch <= 0:
        nxt["decoded_pcs"] = []
        nxt["pc"] = cur["pc"]
    else:
        nxt["decoded_pcs"] = list(range(fetch_pc, fetch_pc + num_fetch))
        nxt["pc"] = fetch_pc + num_fetch


def terminated(state, program):
    return (
        state["pc"] >= len(program)
        and not state["decoded_pcs"]
        and not state["integer_queue"]
        and not state["alu_stage1"]
        and not state["alu_stage2"]
        and not state["active_list"]
        and not state["exception"]
    )


def simulate(program):
    state = initial_state()
    log = [dump_state(state)]

    while not terminated(state, program):
        nxt = copy.deepcopy(state)

        commit(state, nxt)
        alu_forward(state, nxt)
        alu_advance(state, nxt)
        issue(state, nxt)
        dispatched = rename_dispatch(state, nxt, program)
        fetch_decode(state, nxt, program, dispatched)

        state = nxt
        log.append(dump_state(state))

    return log


def main():
    input_file = sys.argv[1]
    output_file = sys.argv[2]

    with open(input_file) as f:
        program_json = json.load(f)

    program = parse_instructions(program_json)
    log = simulate(program)

    with open(output_file, "w") as f:
        json.dump(log, f, indent=2)


if __name__ == "__main__":
    main()

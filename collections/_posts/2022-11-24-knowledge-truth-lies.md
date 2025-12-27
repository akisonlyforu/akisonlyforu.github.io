---
layout:     post    
title:      Knowledge Truth and Lies in Distributed Systems    
date:       2022-11-24    
summary:    Distributed systems cannot always rely on perfect assumptions; explore the philosophical nuances of truth, majority decisions, Byzantine faults, and practical system models.    
categories: ddia distributed-systems fault-tolerance system-models
---

### **Introduction: What Do We Know to Be True?**

In distributed systems, absolute truth is elusive. Nodes in a network have no shared memory and rely solely on message-passing for information exchange. Network partitions, unreliable message delivery, and node outages complicate the scenario further. This makes it nearly impossible to be certain whether the system is functioning correctly or if it's suffering failures.

The unreliable mechanisms in distributed systems force us to redefine certainty and truth. As engineers, our task is to build systems that meet user expectations despite frequent and unpredictable faults.
  
---

### **The Truth Defined by the Majority**

When decisions need to be made in a distributed setting, many algorithms resort to a **quorum**—a mechanism in which most participating nodes agree on an outcome.

#### **Example of Asymmetric Faults**
Imagine a node in the network that can receive messages but cannot send responses back due to a fault. Even if the semi-disconnected node is operating correctly, the other nodes in the system might declare it as failed because they can no longer communicate with it. From the perspective of the majority, the node is “dead,” and it is no longer part of the system despite its protests.

Quorum-based mechanisms rely on making decisions with votes. For example:
- A quorum (e.g., N/2 +1 nodes) determines whether to elect a new leader in a system.
- If the majority of nodes declare a participant as failed, that node is considered "dead" even if it’s still alive and functional.

This model is practical because decisions are driven by the collective agreement of nodes rather than relying on individual judgments.
  
---

### **Byzantine Faults: When Nodes Lie**

While quorum mechanisms assume nodes to be honest but fallible, real-world systems often encounter scenarios where nodes may actively behave maliciously or unpredictably. These are called **Byzantine faults**, named after the **Byzantine Generals Problem** where generals in different locations must coordinate an attack but cannot be sure whether a messenger is delivering false information.

- A Byzantine fault occurs when a node sends incorrect or contradictory information to different parts of the system (e.g., a compromised node pretending to acknowledge writes it hasn’t actually processed).
- Handling such faults is inherently complex, as it’s difficult to distinguish between a genuinely faulty node and a malicious actor.

#### **Approaches to Handle Byzantine Faults**
1. **Consensus Algorithms with Supermajority**: Byzantine fault-tolerant (BFT) systems require more than two-thirds of nodes to function correctly to ensure the system’s integrity. Examples include systems used in **blockchains** like Bitcoin or Ethereum.
2. **Cost of BFT**: Distributed systems in controlled environments (datacenters) avoid Byzantine fault-tolerant algorithms because they are expensive and limit scalability. Instead, hardware or software checks, such as firewalls and access controls, protect against malicious behavior.

---

### **System Models and Reality**

To reason about distributed systems, engineers define abstract **system models** that simplify reality into manageable assumptions. Common models include:
1. **Crash-Stop Faults**: Nodes may fail by crashing and never recovering.
2. **Crash-Recovery Faults**: Nodes may crash but resume after some time, with persisted storage intact.
3. **Byzantine Faults**: Nodes can behave arbitrarily, even maliciously.

Similarly, timing behavior is modeled as:
- **Synchronous Systems**: Network delays, downtime, and clock drift have predictable and bounded upper limits. This model is typically unrealistic for large-scale systems.
- **Asynchronous Systems**: No timing assumptions are made; timeouts must be baked into the protocols.
- **Partially Synchronous Systems**: An unpredictable mix of synchronous and asynchronous behavior, which is often the most practical and widely applicable model for distributed systems.

---

### **Safety and Liveness Properties**

When designing algorithms for distributed systems, correctness is often specified in terms of:
1. **Safety Properties**: Ensure that “nothing bad happens.” For instance, a system ensures that replicas never diverge into different states.
2. **Liveness Properties**: Ensure that “something good eventually happens.” For example, a request to write data to a quorum eventually succeeds if the system stabilizes after a temporary fault.

These properties ensure balanced trade-offs between uptime and consistency.
   
---

### **Conclusion**

Distributed systems challenge our assumptions about knowledge and truth. In the face of node failures, Byzantine faults, and network partitions, achieving correctness requires careful design and abstract reasoning. Concepts like quorum consensus, safety-liveness trade-offs, and robust system models provide practical frameworks to build systems that withstand chaos while meeting user expectations.

As engineers, we must design systems that embrace uncertainty, trusting collective behavior over individual components to uphold reliability and consistency.  
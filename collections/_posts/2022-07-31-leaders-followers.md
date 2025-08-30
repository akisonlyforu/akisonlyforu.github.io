---
layout:     post    
title:      "Leaders and Followers - The Core of Replication"    
date:       2022-07-31
summary:    Dive into the fundamental model of leader-based replication, exploring how data integrity and availability are managed through leaders and followers.    
categories: replication databases distributed-systems ddia
---

### **Introduction to Leader and Follower Replication**

Replication ensures multiple copies of data are stored across different systems for durability, reliability, and availability. The **leader-follower model** (also called **master-slave replication** or **active/passive replication**) serves as one of the most prevalent replication architectures. In this approach, **one node is designated as the “leader”**, and other nodes act as **followers** of this leader.
   
---  

### **How Leader-Based Replication Works**

1. **Leader for Writes**:    
   All write operations flow directly to the leader. The leader updates its local storage before propagating these changes to the followers in a **replication log** or **change stream**.

2. **Followers for Reads**:    
   Followers replicate data changes by consuming the replication log from the leader. In follower replicas, changes are **applied sequentially** in the same order they were processed by the leader.

3. **Resulting Benefits**:
    - Write operations are consistent because only the leader accepts them.
    - Read operations can be load-balanced by querying follower replicas, improving system scalability.

---  

### **Synchronous vs. Asynchronous Replication**

A major aspect of replication architecture is whether it operates in either **synchronous** or **asynchronous mode**:

#### 1. **Synchronous Replication**
- Followers must confirm receipt of a write before the leader acknowledges success to the client.
- **Advantage**: Guarantees consistency between the leader and followers. If the leader fails, followers have an up-to-date copy of the data.
- **Disadvantage**: Systems become prone to stalling if a synchronous follower is slow or unresponsive.

**Usage Example**: High-priority workloads requiring strict durability (e.g., financial systems).

#### 2. **Asynchronous Replication**
- The leader sends changes to followers, but doesn’t wait for confirmation.
- **Advantage**: Leader can continue processing writes without waiting for follower replies, making this mode faster and more resilient to follower outages.
- **Disadvantage**: Followers may lag behind the leader, especially under high workloads or network conditions, leading to temporary inconsistency.

**Usage Example**: Read-heavy workloads prioritizing availability over strict consistency, such as web applications.

Representation:
```plaintext  
+---------+        Write Request       +----------+  
|  Client | -------------------------> |  Leader  |  
+---------+                           / +----------+  
       |                            /          |  
       |  Asynchronous Replication /   Synchronous Acknowledgment  
       ↓                         ↓              ↓  
+-------------+           +-------------+   +-------------+  
| Follower #1 |           | Follower #2 |   | Follower #3 |  
+-------------+           +-------------+   +-------------+  
```  
   
---  

### **Setting Up New Followers**

Creating a new follower involves the following steps:
1. **Snapshot the Leader**: A consistent snapshot of the leader’s database is taken.
2. **Copy Snapshot**: The snapshot is transferred to the new follower node.
3. **Replay Replication Log**: The follower replays any data changes that occurred after the snapshot to catch up to the leader.
4. **Start Update Cycle**: The follower begins consuming live replication updates.

---  

### **Handling Node Failures**

#### Follower Recovery:
If a follower crashes, it can read the replication log from its last known state and replay missed changes to catch up automatically. This **catch-up recovery** minimizes downtime and manual intervention.

#### Leader Failover:
Leader failure requires **promoting a follower to a new leader**, a process known as **failover**:
1. Timeouts detect inactive leaders.
2. Election algorithms select the most up-to-date follower.
3. Clients and remaining followers reconfigure to route updates to the new leader.

Challenges in automatic failover include:
- Handling **"split-brain" scenarios**, where two nodes may simultaneously declare themselves as leaders.
- Managing data loss if replication is asynchronous and the new leader doesn't have the most recent writes.

---  

### **Advantages and Trade-offs**

| **Attribute**         | **Synchronous**             | **Asynchronous**                     |  
|------------------------|----------------------------|--------------------------------------|  
| **Consistency**       | Guaranteed                | Risk of delay in follower updates   |  
| **Durability**        | High                      | May lose data on failure            |  
| **Write Throughput**  | Slower (dependent on followers) | Fast (independent of followers)      |  
| **Read Scalability**  | Moderate                  | High                                |  
   
---  

### **Conclusion**

Leader-based replication offers a robust framework for balancing read and write needs in distributed databases. By understanding the trade-offs of synchronous and asynchronous configurations, operational teams can design resilient systems optimized for system behavior, workloads, and failure handling.

From powering relational databases to distributed message brokers like Kafka, leader-follower replication remains a cornerstone for building scalable and reliable architectures. As the backbone of many modern systems, it’s critical to handle failure modes, lag, and dataflow dynamics effectively.    

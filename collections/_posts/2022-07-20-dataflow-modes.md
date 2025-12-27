---
layout:     post    
title:      Modes of Dataflow in Distributed Systems    
date:       2022-07-20   
summary:    Explore how data flows between processes via databases, RPC services, and asynchronous message brokers, emphasizing compatibility and flexibility.    
categories: dataflow systems messaging ddia
---

In distributed systems, the mode of dataflow determines how information is exchanged between processes that lack shared memory. This exchange involves encoding data into sequences of bytes (serialization) for transmission just as much as decoding it back (deserialization) on the receiving end. The choice of dataflow mode impacts system performance, reliability, and evolvability.
   
---

### **Three Common Modes of Dataflow**

#### 1. **Dataflow Through Databases**

In this mode, processes write to and read from a shared database:
- **Encoding and decoding**: The writing process serializes the data, while the reader deserializes it when accessing the database later.
- **Backward compatibility**: Necessary for ensuring older processes can still read newly written data.
- **Forward compatibility**: Allows older readers encountering newer fields to preserve them when updating records.

A practical challenge arises when an older version of a process writes an updated record without knowing about the new fields added by a newer version. To avoid unintentionally discarding such fields, developers must carefully encode and decode database records during schema evolution.
   
---

#### 2. **Dataflow Through Services (REST and RPC)**

This involves using APIs (usually exposed via HTTP in the case of REST or specialized network protocols for RPC) to directly pass data between processes.
- **REST APIs**:
    - Typically rely on JSON for their payloads.
    - Evolve by making small changes, such as adding optional request/response fields while maintaining backward compatibility.

Example REST snippet:
```http  
GET /api/order/123  
Accept: application/json  
```  

- **RPC Frameworks (e.g., gRPC, Thrift)**:
    - Use schemas (e.g., Protocol Buffers) to strictly define interface specifications.
    - Offer stronger type checking and performance compared to REST, but demand serialized compatibility between servers and clients.

One challenge with services is ensuring compatibility when the server updates before the clients during deployment. By following backward compatibility on requests and forward compatibility on responses, the two can evolve independently without breaking functionality.
   
---

#### 3. **Message-Passing Dataflow (Asynchronous Communication)**

Asynchronous dataflow via a **message broker** (e.g., RabbitMQ, Kafka) builds flexibility and decouples sender and receiver processes. Key traits include:
- **Decoupling**: Senders publish messages without needing to know the identity of the recipients.
- **Reliability improvements**: Brokers buffer messages if recipients are unavailable or overloaded.
- **Consumer models**:
    - **One-to-One Delivery**: Targeted message queues.
    - **One-to-Many Broadcasting**: Publish-subscribe topic models.

Example Workflow with Kafka:
```plaintext  
[Producers → Publish → Kafka Topic → Consumers Process Messages Independently]  
```  

Message brokers, however, typically use opaque payloads that the application layer must parse, meaning evolution depends on backward and forward-compatible encoding formats like Avro or Protocol Buffers.
   
---

### **Strategies for Evolvable Dataflow**

1. **Preservation of Unknown Fields**: Whether working with databases or messages between services, it’s important to avoid discarding fields added by newer processes that aren’t recognized by older versions. This ensures future data integrity in rolling upgrades.
2. **Leverage Versioning**: Employ explicit schema versioning for APIs and payloads to gracefully handle compatibility over time.
3. **Decouple Processing via Queues**: Embrace message brokers to reduce dependency on synchronous systems, ensuring better fault tolerance and ease of scaling independently.

---

### **Conclusion**

The choice of dataflow mode—whether using **databases**, **services**, or **message brokers**—dictates how data processes interact and evolve within a distributed system. Selecting the right approach based on specific use cases while incorporating compatibility strategies ensures flexibility, resilience, and maintainability. As systems grow in complexity, understanding these modes enables engineers to design scalable and robust architectures.  